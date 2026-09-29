//! Guild<->community routing for the multi-tenant guild-pairing model
//! (#500 data-plane half; schema contract:
//! `docs/superpowers/specs/2026-09-29-guild-binding-contract.md`).
//!
//! **Tenant attribution.** The bot/gateway connection that *received* an
//! event determines the tenant -- never the guild id alone, since a
//! shared guild can host N tenants' bots each receiving the same message.
//! Callers (`crate::ingest::discord::run_loop`) own one [`TenantId`] per
//! connection, set once at connect time from that connection's own
//! credentials, and pass it into [`GuildRouter::resolve`] on every
//! message -- this module never infers a tenant from message content.
//!
//! **Community resolution precedence** (contract Sec3), evaluated in
//! order, first match wins:
//! 1. Channel-level binding (`channel_id` matches exactly).
//! 2. Explicit community prefix/tag -- resolved by bundle/command logic
//!    upstream, supplied here as `prefix_hint` (the contract explicitly
//!    does not model this as a table).
//! 3. Guild-level default (`channel_id IS NULL`), only if owned by the
//!    *same* tenant resolving the request.
//! 4. Ambiguous (zero or more than one candidate row) -> fail closed.
//!    Never broadcast to multiple candidates.
//!
//! **Caching.** The contract's `v_guild_routing` view carries no
//! changelog/`safe_seq` table (unlike `bundle_active_set`), so this
//! module uses a bounded TTL (default 30s, [`DEFAULT_CACHE_TTL`]) plus an
//! explicit [`GuildRouter::invalidate`] hook a revocation-aware caller
//! (e.g. a future guild-delete/integration-removed gateway handler) can
//! call to drop a stale entry before its TTL expires. Document this
//! choice if the contract ever grows a changelog table -- at that point
//! this cache should switch to the `changelog_consumer.rs` watermark
//! pattern instead.
//!
//! **Dedupe.** The same Discord message can arrive at this process via
//! more than one tenant's gateway connection (N bots in a shared guild).
//! [`Dedup`] guards at most one processing per `(tenant_id, community_id,
//! message_id)` -- keyed by tenant *and* community, so two tenants
//! independently routing the same message to their own communities is
//! never collapsed into one, and one tenant's dedupe entry can never
//! suppress another tenant's identical key (cross-tenant isolation).
//!
//! **DB wiring (known gap, not silently bridged).** `svc_ingest` has no
//! SeaORM/database dependency today (see `Cargo.toml`'s crate doc,
//! S4.1) -- [`GuildRoutingSource`] is the seam a concrete
//! Postgres-backed implementation plugs into once that dependency is
//! added; [`resolve_precedence`] (the precedence logic itself) is pure
//! and fully unit-tested independent of that wiring. Tracked as
//! remaining work in PR #500's data-plane half.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Default cache TTL for a resolved route -- see module doc "Caching".
pub const DEFAULT_CACHE_TTL: Duration = Duration::from_secs(30);

/// Default dedupe retention window -- long enough to absorb gateway
/// resume/redelivery jitter across a handful of tenant bots without
/// growing unbounded.
pub const DEFAULT_DEDUP_TTL: Duration = Duration::from_secs(300);

/// Opaque tenant identifier -- tenant `"0"` is the reserved global/
/// default tenant (platform bot, contract Sec2); every other value is a
/// paired tenant with its own Discord application.
pub type TenantId = String;

/// One candidate row read from `v_guild_routing` for a given
/// `(tenant_id, platform, guild_id)` -- `channel_id: None` is the
/// guild-level default row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutingRow {
    pub tenant_id: TenantId,
    pub pairing_id: String,
    pub community_id: String,
    pub channel_id: Option<String>,
}

/// Where a resolved route came from -- precedence step, surfaced for
/// logging/metrics labels (never for behavior branching by callers).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingSource {
    ChannelBinding,
    PrefixTag,
    GuildDefault,
}

impl BindingSource {
    pub fn as_metric_label(self) -> &'static str {
        match self {
            BindingSource::ChannelBinding => "channel_binding",
            BindingSource::PrefixTag => "prefix_tag",
            BindingSource::GuildDefault => "guild_default",
        }
    }
}

/// A successfully resolved route for one inbound event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedRoute {
    pub tenant_id: TenantId,
    pub pairing_id: String,
    pub community_id: String,
    pub source: BindingSource,
}

/// Why resolution failed closed -- every variant is a *drop*, never a
/// partial/broadcast fallback. `reason()` is the fail-closed-counter
/// metric label (contract's mandate: "Ambiguous -> fail closed").
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RouteError {
    #[error("no binding resolved for guild")]
    NoBinding,
    #[error("ambiguous binding: {0} candidate rows")]
    Ambiguous(usize),
    #[error("guild default owned by a different tenant")]
    GuildDefaultOwnedByOtherTenant,
    #[error("routing source lookup failed: {0}")]
    SourceError(String),
}

impl RouteError {
    pub fn reason(&self) -> &'static str {
        match self {
            RouteError::NoBinding => "no_binding",
            RouteError::Ambiguous(_) => "ambiguous",
            RouteError::GuildDefaultOwnedByOtherTenant => "guild_default_other_tenant",
            RouteError::SourceError(_) => "source_error",
        }
    }
}

/// Abstraction over reading `v_guild_routing` -- lets [`GuildRouter`] be
/// exercised in tests against a fixed row set with no live Postgres
/// connection. A concrete Postgres-backed implementation (SeaORM raw
/// `SELECT tenant_id, pairing_id, community_id, channel_id FROM
/// v_guild_routing WHERE platform = $1 AND guild_id = $2 AND tenant_id =
/// $3`) is the DB-wiring gap noted in the module doc.
pub trait GuildRoutingSource: Send + Sync {
    /// Every active row for this `(tenant_id, platform, guild_id)` --
    /// both channel-scoped and guild-default rows, so
    /// [`resolve_precedence`] can apply the full precedence chain from
    /// one lookup.
    fn rows_for_guild(
        &self,
        tenant_id: &str,
        platform: &str,
        guild_id: &str,
    ) -> Result<Vec<RoutingRow>, String>;
}

/// Pure precedence resolution (contract Sec3, steps 1/3/4 -- step 2 is
/// the caller-supplied `prefix_hint`) -- no I/O, fully unit-testable.
///
/// `rows` MUST already be scoped to the resolving `tenant_id` (never
/// pass rows belonging to a different tenant in) -- this function itself
/// enforces nothing about tenant scoping beyond the guild-default-
/// ownership check, since [`GuildRoutingSource::rows_for_guild`] takes
/// `tenant_id` as a hard filter, not a hint.
pub fn resolve_precedence(
    rows: &[RoutingRow],
    tenant_id: &str,
    channel_id: Option<&str>,
    prefix_hint: Option<&str>,
) -> Result<ResolvedRoute, RouteError> {
    // Step 1: channel-level binding, exact channel_id match.
    if let Some(cid) = channel_id {
        let matches: Vec<&RoutingRow> = rows
            .iter()
            .filter(|r| r.channel_id.as_deref() == Some(cid))
            .collect();
        match matches.len() {
            0 => {}
            1 => {
                let row = matches[0];
                return Ok(ResolvedRoute {
                    tenant_id: row.tenant_id.clone(),
                    pairing_id: row.pairing_id.clone(),
                    community_id: row.community_id.clone(),
                    source: BindingSource::ChannelBinding,
                });
            }
            n => return Err(RouteError::Ambiguous(n)),
        }
    }

    // Step 2: explicit community prefix/tag, resolved entirely by the
    // caller (bundle/command logic) -- this module just accepts the
    // already-resolved community id as an override at this precedence
    // slot. Not modeled against `rows` since the contract doesn't back
    // it with a table.
    if let Some(community_id) = prefix_hint {
        return Ok(ResolvedRoute {
            tenant_id: tenant_id.to_string(),
            pairing_id: String::new(),
            community_id: community_id.to_string(),
            source: BindingSource::PrefixTag,
        });
    }

    // Step 3: guild-level default (channel_id IS NULL), only if owned by
    // this tenant. Since `rows` is already tenant-scoped by the caller's
    // `rows_for_guild(tenant_id, ...)` lookup, any default row present
    // here is by construction owned by `tenant_id` -- the explicit check
    // is defense-in-depth against a future `rows_for_guild`
    // implementation that stops pre-filtering by tenant.
    let defaults: Vec<&RoutingRow> = rows.iter().filter(|r| r.channel_id.is_none()).collect();
    match defaults.len() {
        0 => Err(RouteError::NoBinding),
        1 => {
            let row = defaults[0];
            if row.tenant_id != tenant_id {
                return Err(RouteError::GuildDefaultOwnedByOtherTenant);
            }
            Ok(ResolvedRoute {
                tenant_id: row.tenant_id.clone(),
                pairing_id: row.pairing_id.clone(),
                community_id: row.community_id.clone(),
                source: BindingSource::GuildDefault,
            })
        }
        n => Err(RouteError::Ambiguous(n)),
    }
}

#[derive(Clone)]
struct CacheEntry {
    inserted_at: Instant,
    result: Result<ResolvedRoute, RouteError>,
}

/// `(tenant_id, platform, guild_id, channel_id)` -- the cache/dedupe key
/// shape, named to satisfy `clippy::type_complexity` rather than inlining
/// the four-tuple at every use site.
type RouteCacheKey = (String, String, String, Option<String>);

/// TTL-cached, fail-closed guild routing resolver. One instance is
/// shared (via `Arc`) across every gateway connection in the process --
/// callers pass their own [`TenantId`] into [`resolve`](Self::resolve)
/// per call, so one `GuildRouter` safely serves multiple tenants'
/// connections without cross-tenant leakage (the cache key includes
/// `tenant_id`).
pub struct GuildRouter<S: GuildRoutingSource> {
    source: S,
    ttl: Duration,
    cache: Mutex<HashMap<RouteCacheKey, CacheEntry>>,
}

impl<S: GuildRoutingSource> GuildRouter<S> {
    pub fn new(source: S) -> Self {
        Self::with_ttl(source, DEFAULT_CACHE_TTL)
    }

    pub fn with_ttl(source: S, ttl: Duration) -> Self {
        Self {
            source,
            ttl,
            cache: Mutex::new(HashMap::new()),
        }
    }

    /// Resolves the community for one inbound event. Fail-closed: any
    /// [`RouteError`] means "drop this event, do not process, do not
    /// broadcast" -- callers must never treat an `Err` as "try every
    /// candidate community instead."
    pub fn resolve(
        &self,
        tenant_id: &str,
        platform: &str,
        guild_id: &str,
        channel_id: Option<&str>,
        prefix_hint: Option<&str>,
    ) -> Result<ResolvedRoute, RouteError> {
        let key = (
            tenant_id.to_string(),
            platform.to_string(),
            guild_id.to_string(),
            channel_id.map(str::to_string),
        );

        if let Some(entry) = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(&key)
        {
            if entry.inserted_at.elapsed() < self.ttl {
                return entry.result.clone();
            }
        }

        let rows = self
            .source
            .rows_for_guild(tenant_id, platform, guild_id)
            .map_err(RouteError::SourceError);

        let result = match rows {
            Ok(rows) => resolve_precedence(&rows, tenant_id, channel_id, prefix_hint),
            Err(err) => Err(err),
        };

        self.cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .insert(
                key,
                CacheEntry {
                    inserted_at: Instant::now(),
                    result: result.clone(),
                },
            );
        result
    }

    /// Explicit invalidation hook for a revocation-aware caller (a
    /// guild-delete/integration-removed gateway event, contract Sec5) --
    /// drops every cached entry for this `(tenant_id, guild_id)` so the
    /// next `resolve` re-reads `v_guild_routing` instead of serving a
    /// stale pre-revocation route for up to [`DEFAULT_CACHE_TTL`].
    pub fn invalidate(&self, tenant_id: &str, guild_id: &str) {
        let mut cache = self
            .cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        cache.retain(|(t, _platform, g, _ch), _| !(t == tenant_id && g == guild_id));
    }
}

/// Bounded-TTL idempotency guard for `(tenant_id, community_id,
/// message_id)` -- see module doc "Dedupe". `check_and_record` returns
/// `true` the first time a key is seen (proceed with processing) and
/// `false` on every subsequent call within [`DEFAULT_DEDUP_TTL`] (drop as
/// a duplicate).
pub struct Dedup {
    ttl: Duration,
    seen: Mutex<HashMap<(String, String, String), Instant>>,
}

impl Default for Dedup {
    fn default() -> Self {
        Self::with_ttl(DEFAULT_DEDUP_TTL)
    }
}

impl Dedup {
    pub fn with_ttl(ttl: Duration) -> Self {
        Self {
            ttl,
            seen: Mutex::new(HashMap::new()),
        }
    }

    /// `true` = first sighting, proceed. `false` = duplicate, drop.
    pub fn check_and_record(&self, tenant_id: &str, community_id: &str, message_id: &str) -> bool {
        let key = (
            tenant_id.to_string(),
            community_id.to_string(),
            message_id.to_string(),
        );
        let mut seen = self
            .seen
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);

        // Lazy sweep of expired entries -- bounded memory without a
        // background task; cheap relative to the gateway message rate
        // this guards.
        let ttl = self.ttl;
        seen.retain(|_, inserted_at| inserted_at.elapsed() < ttl);

        match seen.entry(key) {
            std::collections::hash_map::Entry::Occupied(_) => false,
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert(Instant::now());
                true
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(tenant: &str, pairing: &str, community: &str, channel: Option<&str>) -> RoutingRow {
        RoutingRow {
            tenant_id: tenant.to_string(),
            pairing_id: pairing.to_string(),
            community_id: community.to_string(),
            channel_id: channel.map(str::to_string),
        }
    }

    #[test]
    fn channel_binding_wins_over_guild_default() {
        let rows = vec![
            row("t1", "p1", "community-default", None),
            row("t1", "p1", "community-channel", Some("c1")),
        ];
        let resolved = resolve_precedence(&rows, "t1", Some("c1"), None).unwrap();
        assert_eq!(resolved.source, BindingSource::ChannelBinding);
        assert_eq!(resolved.community_id, "community-channel");
    }

    #[test]
    fn prefix_hint_wins_over_guild_default_when_no_channel_match() {
        let rows = vec![row("t1", "p1", "community-default", None)];
        let resolved =
            resolve_precedence(&rows, "t1", Some("c-unbound"), Some("community-tag")).unwrap();
        assert_eq!(resolved.source, BindingSource::PrefixTag);
        assert_eq!(resolved.community_id, "community-tag");
    }

    #[test]
    fn falls_back_to_guild_default_when_owned_by_same_tenant() {
        let rows = vec![row("t1", "p1", "community-default", None)];
        let resolved = resolve_precedence(&rows, "t1", Some("c-unbound"), None).unwrap();
        assert_eq!(resolved.source, BindingSource::GuildDefault);
        assert_eq!(resolved.community_id, "community-default");
    }

    #[test]
    fn guild_default_owned_by_other_tenant_fails_closed() {
        // rows_for_guild is tenant-scoped by contract, so this exercises
        // the defense-in-depth branch directly.
        let rows = vec![row("t2", "p2", "other-tenant-community", None)];
        let err = resolve_precedence(&rows, "t1", Some("c-unbound"), None).unwrap_err();
        assert_eq!(err, RouteError::GuildDefaultOwnedByOtherTenant);
        assert_eq!(err.reason(), "guild_default_other_tenant");
    }

    #[test]
    fn no_rows_fails_closed_no_binding() {
        let err = resolve_precedence(&[], "t1", Some("c1"), None).unwrap_err();
        assert_eq!(err, RouteError::NoBinding);
        assert_eq!(err.reason(), "no_binding");
    }

    #[test]
    fn ambiguous_channel_rows_fail_closed() {
        let rows = vec![
            row("t1", "p1", "community-a", Some("c1")),
            row("t1", "p1", "community-b", Some("c1")),
        ];
        let err = resolve_precedence(&rows, "t1", Some("c1"), None).unwrap_err();
        assert_eq!(err, RouteError::Ambiguous(2));
    }

    #[test]
    fn ambiguous_guild_defaults_fail_closed() {
        let rows = vec![
            row("t1", "p1", "community-a", None),
            row("t1", "p1", "community-b", None),
        ];
        let err = resolve_precedence(&rows, "t1", None, None).unwrap_err();
        assert_eq!(err, RouteError::Ambiguous(2));
    }

    struct FakeSource {
        rows: Vec<RoutingRow>,
    }

    impl GuildRoutingSource for FakeSource {
        fn rows_for_guild(
            &self,
            tenant_id: &str,
            _platform: &str,
            _guild_id: &str,
        ) -> Result<Vec<RoutingRow>, String> {
            Ok(self
                .rows
                .iter()
                .filter(|r| r.tenant_id == tenant_id)
                .cloned()
                .collect())
        }
    }

    #[test]
    fn cross_tenant_isolation_same_guild_different_tenants() {
        // A shared guild: tenant A has a channel binding, tenant B has a
        // guild default -- tenant B's bot receiving the same guild/
        // channel must never resolve tenant A's community.
        let source = FakeSource {
            rows: vec![
                row("tenant-a", "pair-a", "community-a", Some("c1")),
                row("tenant-b", "pair-b", "community-b", None),
            ],
        };
        let router = GuildRouter::new(source);

        let a = router
            .resolve("tenant-a", "discord", "guild-shared", Some("c1"), None)
            .unwrap();
        assert_eq!(a.community_id, "community-a");

        let b = router
            .resolve("tenant-b", "discord", "guild-shared", Some("c1"), None)
            .unwrap();
        assert_eq!(b.community_id, "community-b");
        assert_eq!(b.source, BindingSource::GuildDefault);
        assert_ne!(a.community_id, b.community_id);
    }

    #[test]
    fn cache_hits_do_not_requery_source() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct CountingSource {
            calls: AtomicUsize,
        }
        impl GuildRoutingSource for CountingSource {
            fn rows_for_guild(
                &self,
                _tenant_id: &str,
                _platform: &str,
                _guild_id: &str,
            ) -> Result<Vec<RoutingRow>, String> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Ok(vec![row("t1", "p1", "community-default", None)])
            }
        }

        let router = GuildRouter::with_ttl(
            CountingSource {
                calls: AtomicUsize::new(0),
            },
            Duration::from_secs(60),
        );
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        assert_eq!(router.source.calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn invalidate_forces_requery() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct CountingSource {
            calls: AtomicUsize,
        }
        impl GuildRoutingSource for CountingSource {
            fn rows_for_guild(
                &self,
                _tenant_id: &str,
                _platform: &str,
                _guild_id: &str,
            ) -> Result<Vec<RoutingRow>, String> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Ok(vec![row("t1", "p1", "community-default", None)])
            }
        }

        let router = GuildRouter::with_ttl(
            CountingSource {
                calls: AtomicUsize::new(0),
            },
            Duration::from_secs(60),
        );
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        router.invalidate("t1", "g1");
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        assert_eq!(router.source.calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn ttl_expiry_forces_requery() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::thread::sleep;

        struct CountingSource {
            calls: AtomicUsize,
        }
        impl GuildRoutingSource for CountingSource {
            fn rows_for_guild(
                &self,
                _tenant_id: &str,
                _platform: &str,
                _guild_id: &str,
            ) -> Result<Vec<RoutingRow>, String> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Ok(vec![row("t1", "p1", "community-default", None)])
            }
        }

        let router = GuildRouter::with_ttl(
            CountingSource {
                calls: AtomicUsize::new(0),
            },
            Duration::from_millis(10),
        );
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        sleep(Duration::from_millis(30));
        router.resolve("t1", "discord", "g1", None, None).unwrap();
        assert_eq!(router.source.calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn dedupe_allows_first_blocks_repeat() {
        let dedup = Dedup::default();
        assert!(dedup.check_and_record("t1", "c1", "msg-1"));
        assert!(!dedup.check_and_record("t1", "c1", "msg-1"));
    }

    #[test]
    fn dedupe_never_crosses_tenants() {
        let dedup = Dedup::default();
        assert!(dedup.check_and_record("tenant-a", "c1", "msg-1"));
        // Same community+message id under a different tenant must be
        // treated as an independent event, never suppressed.
        assert!(dedup.check_and_record("tenant-b", "c1", "msg-1"));
    }

    #[test]
    fn dedupe_never_crosses_communities() {
        let dedup = Dedup::default();
        assert!(dedup.check_and_record("t1", "community-a", "msg-1"));
        assert!(dedup.check_and_record("t1", "community-b", "msg-1"));
    }

    #[test]
    fn dedupe_expires_after_ttl() {
        use std::thread::sleep;
        let dedup = Dedup::with_ttl(Duration::from_millis(10));
        assert!(dedup.check_and_record("t1", "c1", "msg-1"));
        sleep(Duration::from_millis(30));
        assert!(dedup.check_and_record("t1", "c1", "msg-1"));
    }
}
