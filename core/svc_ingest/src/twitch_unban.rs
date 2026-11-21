//! Twitch-native unban intake: EventSub `channel.unban` on the webhook receiver.
//!
//! A moderator who unbans someone in Twitch's own UI must lift the matching Waddles floor ban.
//! Twitch delivers `channel.unban` to the per-subscription EventSub webhook; this module turns a
//! *verified* delivery into a signed event on a dedicated Valkey list that svc-action consumes
//! (`svc_action::twitch_unban`), exactly like the Discord path (`crate::platform_unban`):
//!
//! ```text
//!  webhook POST /eventsub/twitch/webhook/{callback_key}
//!    -> ledger lookup (hub-api): callback_key -> secret + (tenant, community, connection)
//!    -> HMAC over the raw body with THAT subscription's secret   [twitch_eventsub::handle_webhook]
//!    -> subscription/event broadcaster agree; the broadcaster is the one bound to
//!       that tenant/community/connection (hub-api binding list)
//!    -> live state: Helix GET /moderation/banned says the user is NOT banned (fail closed)
//!    -> user id -> user UUID                                       [hub-api tokenizer]
//!    -> Ed25519 sign -> LPUSH waddles:platform-unban:twitch:events
//! ```
//!
//! **Subscription binding (the trust anchor).** The verifying secret is the receiving
//! subscription's own (ledger), never a global one, and the tenant / community / connection that
//! the signed event carries come from that same ledger row -- never from the payload. A valid
//! signature from tenant X's subscription naming tenant Y's broadcaster is dropped and counted
//! (`broadcaster_mismatch`), never forwarded.
//!
//! **Observed time.** `observed_ms` is the EventSub `Twitch-Eventsub-Message-Timestamp` (covered
//! by the HMAC), an authoritative event time, not the receipt time. The lift (SQL) only applies to
//! bans armed at or before it, so a re-ban after the unban survives a late delivery.
//!
//! **Live state.** Before forwarding, Twitch's CURRENT state is read
//! (`GET /moderation/banned?broadcaster_id&user_id`): a user who is banned again, or a state that
//! cannot be read, is dropped (counted). Never lifted on a guess.
//!
//! **Keys.** Events are signed with the same dedicated Ed25519 key as the Discord path
//! (`PLATFORM_UNBAN_SIGNING_KEY`, held only by svc-ingest; svc-action holds the public half) under
//! a Twitch-specific domain label, so a Discord signature can never stand in for a Twitch event
//! or the reverse. The raw Twitch user id never leaves this module: only the tokenized UUID is on
//! the wire, and logs carry numeric ids and fixed reason codes only.

use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use connection_resolver::{ConnectionKind, ConnectionResolve, SecretString};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::commands_reconcile::BoxFut;
use crate::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use crate::outbound_creds::TwitchCredentialSource;
use crate::platform_unban::{Signer, UnbanSink};
use crate::twitch_sessions::spec::{SessionSpec, CAPABILITY, MAX_PAGES, PAGE_LIMIT};

/// Valkey list the verified events are pushed onto (consumed by svc-action).
pub const EVENTS_LIST: &str = "waddles:platform-unban:twitch:events";
/// Signature domain-separation label; must equal svc-action's.
pub const DOMAIN: &[u8] = b"waddles.platform-unban.twitch.v1";
/// Fixed UUIDv5 namespace for event ids derived from EventSub message ids.
pub const EVENT_ID_NAMESPACE: Uuid = Uuid::from_u128(0x7a3e_5b1c_9d42_4f08_a6c1_2b8e_d4f7_3095);
/// Binding metadata key holding the channel's numeric Helix user id (set by hub-api at bind time).
pub const META_CHANNEL_ID: &str = "channel_id";
/// Wire platform of the event.
pub const PLATFORM: &str = "twitch";
/// EventSub subscription type this module handles.
pub const EVENT_TYPE: &str = "channel.unban";
/// Lifetime of an event: svc-action refuses it after this.
pub const EVENT_TTL_MS: u64 = 5 * 60_000;
/// Cap on the list length; the oldest entries are trimmed.
pub const LIST_MAX_LEN: isize = 10_000;
/// How far in the future an EventSub timestamp may claim to be (clock skew).
const MAX_FUTURE_SKEW_MS: u64 = 120_000;
/// Machine-JWT scope hub-api's eventsub secret route requires.
pub const SECRET_READ_SCOPE: &str = "eventsub:secret-read";
/// Bound on each hub-api / Helix call.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);
/// Helix banned-users endpoint.
const BANNED_URL: &str = "https://api.twitch.tv/helix/moderation/banned";

static UNBAN_TOTAL: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_twitch_unban_total",
            "Twitch EventSub channel.unban deliveries by outcome (received / forwarded / ignored / error) and fixed reason",
        ),
        &["outcome", "reason"],
    )
    .expect("valid metric definition")
});

/// Registers the counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(UNBAN_TOTAL.clone()))
}

/// Count of one `(outcome, reason)` (test/observability helper).
#[must_use]
pub fn count(outcome: &str, reason: &str) -> u64 {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).get()
}

fn note(outcome: &'static str, reason: &'static str) {
    UNBAN_TOTAL.with_label_values(&[outcome, reason]).inc();
}

fn ignored(reason: &'static str, entry: &LedgerEntry) -> Outcome {
    note("ignored", reason);
    tracing::info!(
        reason,
        tenant_id = entry.tenant_id,
        community_id = entry.community_id,
        connection_id = entry.connection_id,
        "twitch unban event ignored"
    );
    Outcome::Ignored(reason)
}

fn retry(reason: &'static str, entry: &LedgerEntry) -> Outcome {
    note("retry", reason);
    tracing::warn!(
        reason,
        tenant_id = entry.tenant_id,
        community_id = entry.community_id,
        connection_id = entry.connection_id,
        "twitch unban event hit a transient failure; answering 5xx so Twitch redelivers"
    );
    Outcome::Retry(reason)
}

/// What a `channel.unban` delivery came to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Signed and queued for svc-action.
    Forwarded,
    /// Dropped (counted) with a fixed reason; the Waddles ban stays in force.
    Ignored(&'static str),
    /// A transient dependency failure (binding list, ban-state read, tokenizer, queue): the
    /// receiver answers 5xx so Twitch redelivers; nothing was forwarded.
    Retry(&'static str),
}

/// What a callback key is bound to: its secret and the owning tenant/community/connection.
pub struct LedgerEntry {
    /// The subscription's webhook secret (zeroizing, never logged).
    pub secret: SecretString,
    /// Owning tenant (numeric).
    pub tenant_id: i64,
    /// Owning community (numeric).
    pub community_id: i64,
    /// Owning connection (numeric).
    pub connection_id: i64,
}

/// Why a ledger lookup produced no entry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LedgerError {
    /// The ledger could not be reached or answered unusably (retryable).
    Unavailable,
}

/// Source of the per-subscription secret and binding (hub-api's subscription ledger).
pub trait SubscriptionLedger: Send + Sync {
    /// The entry for `callback_key`: `Ok(None)` when no such subscription exists.
    fn lookup<'a>(
        &'a self,
        callback_key: &'a str,
    ) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>>;

    /// Reads the CURRENT ledger row, bypassing any cache (used for `channel.unban`, which must
    /// never be authorized by a stale entry after a revoke/rotate/rebind). Defaults to
    /// [`Self::lookup`] for uncached ledgers.
    fn lookup_fresh<'a>(
        &'a self,
        callback_key: &'a str,
    ) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        self.lookup(callback_key)
    }

    /// Drops any cached answer for `callback_key` so the next lookup re-reads the source (called
    /// on an HMAC failure: the secret may have been rotated). A no-op for uncached ledgers.
    fn invalidate(&self, _callback_key: &str) {}
}

/// How long a found ledger entry is served from memory.
pub const CACHE_TTL: Duration = Duration::from_secs(60);
/// How long "no such subscription" is remembered (short: a new subscription must work quickly).
pub const NEGATIVE_TTL: Duration = Duration::from_secs(5);
/// Most callback keys held at once (bounds memory against key-spraying).
pub const CACHE_MAX: usize = 1024;

/// One cached lookup result.
struct Cached {
    at: std::time::Instant,
    /// `None` = the ledger said there is no such subscription.
    entry: Option<(String, i64, i64, i64)>,
}

/// In-process TTL cache over a [`SubscriptionLedger`]: positive answers for `ttl`, negative ones
/// for `negative_ttl` (a few seconds), bounded to `max` keys. Ledger errors are never cached.
/// [`SubscriptionLedger::invalidate`] evicts a key so a rotated secret is re-fetched once.
pub struct CachedLedger<L> {
    inner: L,
    ttl: Duration,
    negative_ttl: Duration,
    max: usize,
    map: std::sync::Mutex<std::collections::HashMap<String, Cached>>,
    /// Per-key gate so concurrent fresh reads of one key coalesce into a single hub-api call.
    flights: std::sync::Mutex<std::collections::HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// Per-key record of the last GATED fresh read, stamped with when its hub-api request STARTED.
    /// Written only by [`SubscriptionLedger::lookup_fresh`]; never by the plain cached path.
    generations: std::sync::Mutex<std::collections::HashMap<String, Generation>>,
    /// Last time a key's cache entry was evicted for a rotation retry (rate-limits eviction).
    evicted: std::sync::Mutex<std::collections::HashMap<String, std::time::Instant>>,
}

/// One fresh read's answer and the instant its hub-api request started.
struct Generation {
    started: std::time::Instant,
    entry: Option<(String, i64, i64, i64)>,
}

/// Minimum spacing between rotation-retry evictions of the same key: a flood of forged
/// signatures on a known key costs at most one extra hub-api read per this interval.
pub const EVICT_MIN_INTERVAL: Duration = Duration::from_secs(5);

impl<L: SubscriptionLedger> CachedLedger<L> {
    /// Wraps `inner` with the standard TTLs and bound.
    #[must_use]
    pub fn new(inner: L) -> Self {
        Self::with_limits(inner, CACHE_TTL, NEGATIVE_TTL, CACHE_MAX)
    }

    /// Wraps `inner` with explicit limits (tests).
    #[must_use]
    pub fn with_limits(inner: L, ttl: Duration, negative_ttl: Duration, max: usize) -> Self {
        Self {
            inner,
            ttl,
            negative_ttl,
            max: max.max(1),
            map: std::sync::Mutex::default(),
            flights: std::sync::Mutex::default(),
            generations: std::sync::Mutex::default(),
            evicted: std::sync::Mutex::default(),
        }
    }

    fn fresh(&self, key: &str) -> Option<Option<LedgerEntry>> {
        let map = self.map.lock().ok()?;
        let c = map.get(key)?;
        let ttl = if c.entry.is_some() {
            self.ttl
        } else {
            self.negative_ttl
        };
        if c.at.elapsed() >= ttl {
            return None;
        }
        Some(c.entry.as_ref().map(|(sec, t, co, cn)| LedgerEntry {
            secret: SecretString::new(sec.clone()),
            tenant_id: *t,
            community_id: *co,
            connection_id: *cn,
        }))
    }

    /// The last gated fresh answer for `key` when its request started at or after `since`.
    fn generation_since(
        &self,
        key: &str,
        since: std::time::Instant,
    ) -> Option<Option<LedgerEntry>> {
        let gens = self.generations.lock().ok()?;
        let g = gens.get(key)?;
        if g.started < since {
            return None;
        }
        Some(g.entry.as_ref().map(|(sec, t, co, cn)| LedgerEntry {
            secret: SecretString::new(sec.clone()),
            tenant_id: *t,
            community_id: *co,
            connection_id: *cn,
        }))
    }

    fn record_generation(
        &self,
        key: &str,
        started: std::time::Instant,
        entry: Option<&LedgerEntry>,
    ) {
        let Ok(mut gens) = self.generations.lock() else {
            return;
        };
        if gens.len() >= self.max && !gens.contains_key(key) {
            gens.clear();
        }
        gens.insert(
            key.to_string(),
            Generation {
                started,
                entry: entry.map(|e| {
                    (
                        e.secret.expose().to_string(),
                        e.tenant_id,
                        e.community_id,
                        e.connection_id,
                    )
                }),
            },
        );
    }

    fn store(&self, key: &str, entry: Option<&LedgerEntry>) {
        let Ok(mut map) = self.map.lock() else {
            return;
        };
        if map.len() >= self.max && !map.contains_key(key) {
            let ttl = self.ttl;
            map.retain(|_, c| c.at.elapsed() < ttl);
            if map.len() >= self.max {
                map.clear();
            }
        }
        map.insert(
            key.to_string(),
            Cached {
                at: std::time::Instant::now(),
                entry: entry.map(|e| {
                    (
                        e.secret.expose().to_string(),
                        e.tenant_id,
                        e.community_id,
                        e.connection_id,
                    )
                }),
            },
        );
    }
}

impl<L: SubscriptionLedger> SubscriptionLedger for CachedLedger<L> {
    fn lookup<'a>(
        &'a self,
        callback_key: &'a str,
    ) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        Box::pin(async move {
            if let Some(hit) = self.fresh(callback_key) {
                return Ok(hit);
            }
            let found = self.inner.lookup(callback_key).await?;
            self.store(callback_key, found.as_ref());
            Ok(found)
        })
    }

    fn lookup_fresh<'a>(
        &'a self,
        callback_key: &'a str,
    ) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        Box::pin(async move {
            // Single-flight: callers that queue behind an in-progress read of this key reuse its
            // result when it finished after they asked (so it is at least as fresh as theirs).
            let asked = std::time::Instant::now();
            let gate = {
                let mut flights = self.flights.lock().map_err(|_| LedgerError::Unavailable)?;
                if flights.len() >= self.max && !flights.contains_key(callback_key) {
                    flights.retain(|_, g| Arc::strong_count(g) > 1);
                }
                Arc::clone(flights.entry(callback_key.to_string()).or_default())
            };
            let _held = gate.lock().await;
            // Reuse a gated fresh read ONLY if its hub-api request started at or after this
            // caller asked (so it cannot predate a revocation the caller already knows of).
            if let Some(hit) = self.generation_since(callback_key, asked) {
                return Ok(hit);
            }
            let started = std::time::Instant::now();
            let found = self.inner.lookup_fresh(callback_key).await?;
            self.record_generation(callback_key, started, found.as_ref());
            // Refresh the cache with what hub-api just said (a revoked row stops being served).
            self.store(callback_key, found.as_ref());
            Ok(found)
        })
    }

    fn invalidate(&self, callback_key: &str) {
        if let Ok(mut ev) = self.evicted.lock() {
            if ev.len() >= self.max {
                ev.retain(|_, t| t.elapsed() < EVICT_MIN_INTERVAL);
                if ev.len() >= self.max {
                    ev.clear();
                }
            }
            if ev
                .get(callback_key)
                .is_some_and(|t| t.elapsed() < EVICT_MIN_INTERVAL)
            {
                return;
            }
            ev.insert(callback_key.to_string(), std::time::Instant::now());
        }
        if let Ok(mut map) = self.map.lock() {
            map.remove(callback_key);
        }
        self.inner.invalidate(callback_key);
    }
}

#[derive(Deserialize)]
struct LedgerBody {
    #[serde(default)]
    secret: String,
    #[serde(default)]
    tenant_id: i64,
    #[serde(default)]
    community_id: i64,
    #[serde(default)]
    connection_id: i64,
}

/// Production [`SubscriptionLedger`]: hub-api over HTTP with a machine JWT of scope
/// [`SECRET_READ_SCOPE`]. Uncached by itself; production wraps it in [`CachedLedger`].
pub struct HubSubscriptionLedger {
    http: reqwest::Client,
    base: String,
    jwt: Arc<service_auth::MachineJwtClient>,
}

impl HubSubscriptionLedger {
    /// `base` is the hub-api root; `jwt` must mint [`SECRET_READ_SCOPE`].
    ///
    /// # Errors
    /// The `reqwest` client builder error.
    pub fn new(
        base: &str,
        jwt: Arc<service_auth::MachineJwtClient>,
    ) -> Result<Self, reqwest::Error> {
        Ok(Self {
            http: reqwest::Client::builder().timeout(CALL_TIMEOUT).build()?,
            base: base.trim_end_matches('/').to_string(),
            jwt,
        })
    }
}

/// A short, non-reversible tag of a callback key for logs (never the full key).
#[must_use]
pub fn key_tag(key: &str) -> String {
    key.chars().take(4).collect::<String>() + "..."
}

/// A callback key is an opaque URL-safe token; anything else never reaches the ledger.
fn is_callback_key(k: &str) -> bool {
    (8..=64).contains(&k.len())
        && k.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

impl SubscriptionLedger for HubSubscriptionLedger {
    fn lookup<'a>(
        &'a self,
        callback_key: &'a str,
    ) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        Box::pin(async move {
            if !is_callback_key(callback_key) {
                return Ok(None);
            }
            let token = self
                .jwt
                .get_token()
                .await
                .map_err(|_| LedgerError::Unavailable)?;
            let resp = self
                .http
                .get(format!(
                    "{}/api/v1/internal/eventsub/secret/{callback_key}",
                    self.base
                ))
                .bearer_auth(&*token)
                .send()
                .await
                .map_err(|_| LedgerError::Unavailable)?;
            match resp.status().as_u16() {
                200 => {}
                404 => return Ok(None),
                _ => return Err(LedgerError::Unavailable),
            }
            let body: LedgerBody = resp.json().await.map_err(|_| LedgerError::Unavailable)?;
            if body.secret.is_empty()
                || body.tenant_id < 1
                || body.community_id < 1
                || body.connection_id < 1
            {
                return Err(LedgerError::Unavailable);
            }
            Ok(Some(LedgerEntry {
                secret: SecretString::new(body.secret),
                tenant_id: body.tenant_id,
                community_id: body.community_id,
                connection_id: body.connection_id,
            }))
        })
    }
}

/// Why the bound broadcaster of a connection could not be established.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OwnerError {
    /// No such connection binding in that community.
    NotFound,
    /// The connection belongs to a different tenant than the subscription's ledger row.
    TenantMismatch,
    /// hub-api could not answer (retryable).
    Unavailable,
}

/// The channel a tenant/community/connection is bound to (hub-api binding list).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BoundChannel {
    /// Canonical (lowercase) channel login. MUTABLE (a rename changes it): used only to resolve
    /// the community's credential, never to decide whether an event belongs to the channel.
    pub login: String,
    /// The numeric, immutable Helix user id hub-api resolved and stored when the binding was
    /// created (`GET /users?login=` with the tenant's credential, failing loud). `None` for a
    /// binding that predates it: the unban is then refused (fail closed).
    pub channel_id: Option<String>,
}

/// The channel a tenant/community/connection is bound to (hub-api binding list).
pub trait ConnectionOwner: Send + Sync {
    /// The bound channel of the connection.
    fn bound_channel<'a>(
        &'a self,
        tenant_id: i64,
        community_id: i64,
        connection_id: i64,
    ) -> BoxFut<'a, Result<BoundChannel, OwnerError>>;
}

/// Production [`ConnectionOwner`] over the connection resolver's binding list.
pub struct ResolverOwner<R: ConnectionResolve> {
    resolver: R,
}

impl<R: ConnectionResolve> ResolverOwner<R> {
    /// Wraps a resolver.
    #[must_use]
    pub fn new(resolver: R) -> Self {
        Self { resolver }
    }
}

impl<R: ConnectionResolve> ConnectionOwner for ResolverOwner<R> {
    fn bound_channel<'a>(
        &'a self,
        tenant_id: i64,
        community_id: i64,
        connection_id: i64,
    ) -> BoxFut<'a, Result<BoundChannel, OwnerError>> {
        Box::pin(async move {
            let mut after = 0i64;
            for _ in 0..MAX_PAGES {
                let page = tokio::time::timeout(
                    CALL_TIMEOUT,
                    self.resolver
                        .list_bindings("twitch", Some(CAPABILITY), after, PAGE_LIMIT),
                )
                .await
                .map_err(|_| OwnerError::Unavailable)?
                .map_err(|_| OwnerError::Unavailable)?;
                for b in &page.items {
                    if b.community_id == community_id
                        && b.connection_id == connection_id
                        && b.kind == ConnectionKind::PlatformOauth
                    {
                        if b.tenant_id != tenant_id {
                            return Err(OwnerError::TenantMismatch);
                        }
                        let login = SessionSpec::from_summary(b)
                            .map(|s| s.channel)
                            .map_err(|_| OwnerError::NotFound)?;
                        let channel_id = b
                            .metadata
                            .get(META_CHANNEL_ID)
                            .filter(|id| is_twitch_id(id))
                            .cloned();
                        return Ok(BoundChannel { login, channel_id });
                    }
                }
                match page.next_after {
                    Some(next) if next > after => after = next,
                    Some(_) => return Err(OwnerError::Unavailable),
                    None => return Err(OwnerError::NotFound),
                }
            }
            Err(OwnerError::Unavailable)
        })
    }
}

/// Twitch's CURRENT ban state of a user in a channel.
pub trait BanState: Send + Sync {
    /// `Ok(true)` when the user is banned (or timed out) now; `Err` when it cannot be read.
    fn is_banned<'a>(
        &'a self,
        community_id: i64,
        channel_login: &'a str,
        broadcaster_id: &'a str,
        user_id: &'a str,
    ) -> BoxFut<'a, Result<bool, ()>>;
}

#[derive(Deserialize)]
struct BannedBody {
    data: Vec<serde_json::Value>,
}

/// Production [`BanState`]: Helix `GET /moderation/banned` with the community's own OAuth token.
pub struct HelixBanState<C: TwitchCredentialSource> {
    creds: C,
    client_id: String,
    client: bundle_host_http::guarded::GuardedClient,
    url: String,
}

impl<C: TwitchCredentialSource> HelixBanState<C> {
    /// Production wiring against the real Helix endpoint.
    #[must_use]
    pub fn new(creds: C, client_id: String) -> Self {
        Self::with_endpoint(
            creds,
            client_id,
            bundle_host_http::guarded::GuardedClient::new(
                bundle_host_http::guarded::GuardedLimits::default(),
                Vec::new(),
            ),
            BANNED_URL.to_string(),
        )
    }

    /// Explicit endpoint + client (tests point it at a local fake).
    #[must_use]
    pub fn with_endpoint(
        creds: C,
        client_id: String,
        client: bundle_host_http::guarded::GuardedClient,
        url: String,
    ) -> Self {
        Self {
            creds,
            client_id,
            client,
            url,
        }
    }
}

impl<C: TwitchCredentialSource> HelixBanState<C> {
    /// Reads Twitch's current ban state of `user_id` in `broadcaster_id`'s channel with `token`
    /// (the community's own OAuth token). `Ok(true)` when banned or timed out; any transport,
    /// status or parse failure is `Err` (fail closed).
    pub async fn state_with_token(
        &self,
        token: &str,
        broadcaster_id: &str,
        user_id: &str,
    ) -> Result<bool, ()> {
        let mut url = reqwest::Url::parse(&self.url).map_err(|_| ())?;
        url.query_pairs_mut()
            .append_pair("broadcaster_id", broadcaster_id)
            .append_pair("user_id", user_id);
        let resp = self
            .client
            .fetch(
                reqwest::Method::GET,
                url.as_str(),
                vec![
                    ("Authorization".to_string(), format!("Bearer {token}")),
                    ("Client-Id".to_string(), self.client_id.clone()),
                ],
                None,
            )
            .await
            .map_err(|_| ())?;
        if !(200..300).contains(&resp.status) {
            return Err(());
        }
        let body: BannedBody = serde_json::from_slice(&resp.body).map_err(|_| ())?;
        Ok(!body.data.is_empty())
    }
}

/// Production wiring (concrete resolver: the credential trait's futures are only provably `Send`
/// for a concrete source, which the object-safe [`BanState`] needs).
impl BanState
    for HelixBanState<
        crate::outbound_creds::ResolverTwitchCredentials<connection_resolver::ConnectionResolver>,
    >
{
    fn is_banned<'a>(
        &'a self,
        community_id: i64,
        channel_login: &'a str,
        broadcaster_id: &'a str,
        user_id: &'a str,
    ) -> BoxFut<'a, Result<bool, ()>> {
        Box::pin(async move {
            let cred = self
                .creds
                .credential(Some(community_id), &format!("#{channel_login}"))
                .await
                .map_err(|_| ())?;
            self.state_with_token(cred.oauth_token.expose(), broadcaster_id, user_id)
                .await
        })
    }
}

/// The exact bytes signed: the domain label, then each field length-prefixed (u32 BE); numbers as
/// decimal text. Must equal svc-action's `twitch_unban::signing_bytes`.
#[must_use]
#[allow(clippy::too_many_arguments)] // the signed field set is the contract; one param per field
pub fn signing_bytes(
    event_id: &str,
    tenant_id: i64,
    community_id: i64,
    connection_id: i64,
    user_uuid: &str,
    observed_ms: u64,
    exp_ms: u64,
) -> Vec<u8> {
    let nums = [
        tenant_id.to_string(),
        community_id.to_string(),
        connection_id.to_string(),
        observed_ms.to_string(),
        exp_ms.to_string(),
    ];
    let parts: [&str; 8] = [
        event_id, &nums[0], &nums[1], &nums[2], user_uuid, &nums[3], &nums[4], PLATFORM,
    ];
    let mut out = DOMAIN.to_vec();
    for part in parts {
        out.extend_from_slice(&u32::try_from(part.len()).unwrap_or(u32::MAX).to_be_bytes());
        out.extend_from_slice(part.as_bytes());
    }
    out
}

/// The signed event as it is placed on [`EVENTS_LIST`].
#[derive(Debug, Serialize)]
pub struct WireEvent<'a> {
    /// Wire version.
    pub v: u32,
    /// Unique event id (single-use marker on the consumer side).
    pub event_id: &'a str,
    /// Tenant of the RECEIVING subscription (ledger), not a payload field.
    pub tenant_id: i64,
    /// Community of the receiving subscription (ledger).
    pub community_id: i64,
    /// Connection of the receiving subscription (ledger).
    pub connection_id: i64,
    /// Always `twitch`.
    pub platform: &'a str,
    /// The tokenized user UUID.
    pub user_uuid: &'a str,
    /// EventSub message timestamp, epoch ms.
    pub observed_ms: u64,
    /// Deadline, epoch ms.
    pub exp_ms: u64,
    /// Signing key id.
    pub kid: &'a str,
    /// Base64 Ed25519 signature over [`signing_bytes`].
    pub sig: &'a str,
}

/// Valkey-backed sink for the Twitch list: `LPUSH` + `LTRIM` (bounded), consumed with `BRPOP`.
pub struct ValkeyListSink {
    conn: redis::aio::ConnectionManager,
}

impl ValkeyListSink {
    /// Wraps a connected manager.
    #[must_use]
    pub fn new(conn: redis::aio::ConnectionManager) -> Self {
        Self { conn }
    }
}

impl UnbanSink for ValkeyListSink {
    fn push<'a>(&'a self, payload: String) -> BoxFut<'a, Result<(), ()>> {
        Box::pin(async move {
            let mut conn = self.conn.clone();
            redis::pipe()
                .cmd("LPUSH")
                .arg(EVENTS_LIST)
                .arg(payload)
                .ignore()
                .cmd("LTRIM")
                .arg(EVENTS_LIST)
                .arg(0)
                .arg(LIST_MAX_LEN - 1)
                .ignore()
                .query_async::<()>(&mut conn)
                .await
                .map_err(|_| ())
        })
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// `true` for 1..=20 ASCII digits (a Twitch numeric user id).
fn is_twitch_id(s: &str) -> bool {
    (1..=20).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}

/// Parses the EventSub message timestamp (RFC 3339) to epoch ms.
fn observed_ms_of(ts: &str) -> Option<u64> {
    let ms = chrono::DateTime::parse_from_rfc3339(ts)
        .ok()?
        .timestamp_millis();
    u64::try_from(ms).ok().filter(|m| *m > 0)
}

/// Turns a verified, ledger-bound `channel.unban` delivery into a signed event for svc-action.
pub struct TwitchUnban {
    ledger: Arc<dyn SubscriptionLedger>,
    owner: Arc<dyn ConnectionOwner>,
    ban_state: Arc<dyn BanState>,
    tokenizer: Arc<dyn IdentityTokenizer>,
    sink: Arc<dyn UnbanSink>,
    signer: Option<Signer>,
}

impl TwitchUnban {
    /// Builds the forwarder. `signer = None` means every event is ignored loudly
    /// (`no_signing_key`) rather than sent unsigned.
    #[must_use]
    pub fn new(
        ledger: Arc<dyn SubscriptionLedger>,
        owner: Arc<dyn ConnectionOwner>,
        ban_state: Arc<dyn BanState>,
        tokenizer: Arc<dyn IdentityTokenizer>,
        sink: Arc<dyn UnbanSink>,
        signer: Option<Signer>,
    ) -> Self {
        Self {
            ledger,
            owner,
            ban_state,
            tokenizer,
            sink,
            signer,
        }
    }

    /// The ledger entry of `callback_key` (the webhook receiver verifies the HMAC with its secret).
    ///
    /// # Errors
    /// [`LedgerError::Unavailable`] when hub-api cannot answer.
    pub async fn ledger_entry(
        &self,
        callback_key: &str,
        fresh: bool,
    ) -> Result<Option<LedgerEntry>, LedgerError> {
        if fresh {
            self.ledger.lookup_fresh(callback_key).await
        } else {
            self.ledger.lookup(callback_key).await
        }
    }

    /// Evicts the cached ledger answer for `callback_key` (HMAC failure: the secret may have
    /// rotated, so the next lookup re-reads hub-api).
    pub fn invalidate_ledger(&self, callback_key: &str) {
        self.ledger.invalidate(callback_key);
    }

    /// Counts a delivery for a subscription the ledger does not know (never forwarded).
    pub fn unbound(&self) -> Outcome {
        note("ignored", "unknown_subscription");
        tracing::warn!("twitch unban event for a subscription the ledger does not know: ignored");
        Outcome::Ignored("unknown_subscription")
    }

    /// Handles one HMAC-verified `channel.unban` notification received on the subscription
    /// `entry`. `subscription` / `event` are the body's objects; `message_timestamp` is the
    /// (HMAC-covered) `Twitch-Eventsub-Message-Timestamp` header.
    pub async fn handle(
        &self,
        entry: &LedgerEntry,
        subscription: &serde_json::Value,
        event: &serde_json::Value,
        message_timestamp: &str,
        message_id: &str,
    ) -> Outcome {
        note("received", "-");
        let text = |v: &serde_json::Value, k: &str| -> Option<String> {
            v.get(k)
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        };
        let (Some(sub_bcast), Some(ev_bcast), Some(user_id)) = (
            subscription
                .get("condition")
                .and_then(|c| text(c, "broadcaster_user_id")),
            text(event, "broadcaster_user_id"),
            text(event, "user_id"),
        ) else {
            return ignored("malformed", entry);
        };
        if !is_twitch_id(&sub_bcast) || !is_twitch_id(&ev_bcast) || !is_twitch_id(&user_id) {
            return ignored("malformed", entry);
        }
        if sub_bcast != ev_bcast {
            return ignored("condition_mismatch", entry);
        }
        // The broadcaster must be the one bound to THIS subscription's tenant/community/
        // connection: a valid signature from another tenant's subscription naming a broadcaster
        // that is not ours is dropped here.
        let bound = match self
            .owner
            .bound_channel(entry.tenant_id, entry.community_id, entry.connection_id)
            .await
        {
            Ok(l) => l,
            Err(OwnerError::NotFound) => return ignored("unknown_broadcaster", entry),
            Err(OwnerError::TenantMismatch) => return ignored("tenant_mismatch", entry),
            Err(OwnerError::Unavailable) => return retry("binding_unavailable", entry),
        };
        // Identity is the immutable numeric id stored on the binding by hub-api, never the
        // mutable login and never a payload value. A binding without it is refused outright.
        let Some(bound_id) = bound.channel_id.as_deref() else {
            return ignored("binding_id_missing", entry);
        };
        if ev_bcast != bound_id {
            return ignored("broadcaster_mismatch", entry);
        }
        let bound = bound.login;
        let Some(observed_ms) = observed_ms_of(message_timestamp) else {
            return ignored("bad_timestamp", entry);
        };
        if observed_ms > now_ms().saturating_add(MAX_FUTURE_SKEW_MS) {
            return ignored("bad_timestamp", entry);
        }
        // Live state: only an unban that is still true is believed.
        match self
            .ban_state
            .is_banned(entry.community_id, &bound, &ev_bcast, &user_id)
            .await
        {
            Ok(false) => {}
            Ok(true) => return ignored("rebanned", entry),
            Err(()) => return retry("ban_state_unavailable", entry),
        }
        let call = CallScope {
            platform: PLATFORM,
            community_id: entry.community_id,
            tenant_id: entry.tenant_id,
            connection_id: entry.connection_id,
        };
        let user_uuid = match self.tokenizer.tokenize(&call, &user_id).await {
            Ok(u) if Uuid::parse_str(&u).is_ok() => u,
            Ok(_) => return ignored("tokenize_invalid", entry),
            Err(TokenizeError::Unavailable | TokenizeError::RateLimited) => {
                return retry("tokenize_unavailable", entry)
            }
            Err(_) => return ignored("tokenize_refused", entry),
        };
        let Some(signer) = &self.signer else {
            tracing::error!(
                tenant_id = entry.tenant_id,
                "twitch unban NOT forwarded: no platform-unban signing key configured"
            );
            return ignored("no_signing_key", entry);
        };
        // Deterministic from Twitch's stable `message_id`: a redelivery maps to the SAME event id,
        // so svc-action's single-use marker dedupes it even if ingest's own dedupe store was down.
        let event_id = Uuid::new_v5(&EVENT_ID_NAMESPACE, message_id.as_bytes()).to_string();
        let exp_ms = now_ms().saturating_add(EVENT_TTL_MS);
        let msg = signing_bytes(
            &event_id,
            entry.tenant_id,
            entry.community_id,
            entry.connection_id,
            &user_uuid,
            observed_ms,
            exp_ms,
        );
        let sig = signer.sign(&msg);
        let wire = WireEvent {
            v: 1,
            event_id: &event_id,
            tenant_id: entry.tenant_id,
            community_id: entry.community_id,
            connection_id: entry.connection_id,
            platform: PLATFORM,
            user_uuid: &user_uuid,
            observed_ms,
            exp_ms,
            kid: signer.kid(),
            sig: &sig,
        };
        let Ok(payload) = serde_json::to_string(&wire) else {
            return ignored("serialize", entry);
        };
        match self.sink.push(payload).await {
            Ok(()) => {
                note("forwarded", "-");
                tracing::info!(
                    tenant_id = entry.tenant_id,
                    community_id = entry.community_id,
                    connection_id = entry.connection_id,
                    "twitch unban event forwarded to svc-action"
                );
                Outcome::Forwarded
            }
            Err(()) => {
                note("error", "publish_failed");
                tracing::error!(
                    tenant_id = entry.tenant_id,
                    community_id = entry.community_id,
                    "ALERT twitch unban event could not be queued; the Waddles ban stays in force"
                );
                Outcome::Retry("publish_failed")
            }
        }
    }
}

#[cfg(test)]
mod tests;
