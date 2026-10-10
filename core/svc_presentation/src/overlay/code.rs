//! Overlay codes: the unguessable public handle in every overlay URL.
//!
//! Overlays used to live at `/overlay/{community_id}/{surface}`, so walking the
//! sequential integer id enumerated every community's overlay. They now live at
//! `/{overlay_code}/{surface}`, where `overlay_code` is a per-community random
//! 64-bit value rendered as 16 lowercase hex characters
//! (`communities.overlay_code`, alembic `0056_communities_overlay_code`).
//!
//! The code is **only a public path handle**. [`OverlayCodeResolver`] turns it
//! into the real `community_id` once, at the edge (the guards in
//! [`crate::overlay::router`]); everything after that -- credentials, scopes,
//! hub fan-out keys, tenant lookup, metrics -- is keyed by that id, exactly as
//! before. The code is not a credential (the VIEW `?key=` / PUSH JWT still gate
//! access) but it is URL-secret material: it is never logged, never put in a
//! span or metric label, and never echoed beyond the response to the request
//! that already carried it.
//!
//! # Lookup and cache
//!
//! [`SeaOrmOverlayCodeResolver`] reads `communities` (read-only) through a
//! small in-memory cache, because a live overlay opens an SSE stream per OBS
//! source and a chat-heavy community pushes many frames per second:
//!
//! * a found mapping is served for [`CODE_TTL`] (default 30s,
//!   `OVERLAY_CODE_CACHE_TTL_SECONDS`) -- this is also the **invalidation
//!   bound**: when hub-api rotates a leaked code the old URL keeps resolving
//!   for at most one TTL (the cache is per-process; hub-api cannot reach into
//!   it), and a stream that was already open stays open until it reconnects;
//! * an absent code (a stale URL or a scanner's guess) is cached for the
//!   shorter [`ABSENT_TTL`], so enumeration probes hit the database at most
//!   once per code per few seconds instead of once per request;
//! * lookup *errors* are never cached;
//! * the map is bounded ([`MAX_CACHED`]); on overflow expired and absent
//!   entries are shed first, so random-code probing cannot evict the real
//!   mappings, and only then is it cleared.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter};
use thiserror::Error;

use crate::db::entities::community_overlay_code::{Column, Entity as CommunityOverlayCode};
use crate::telemetry::OverlayCodeMetrics;

/// Length of an overlay code: 64 random bits as lowercase hex.
pub const CODE_LEN: usize = 16;

/// How long a found code -> community mapping is served from memory. Also the
/// worst-case delay before a rotated (leaked) code stops resolving.
pub const CODE_TTL: Duration = Duration::from_secs(30);

/// Longest an *absent* code is remembered, whatever the configured TTL.
pub const ABSENT_TTL: Duration = Duration::from_secs(5);

/// Upper bound on cached codes. A deployment has far fewer communities; the
/// bound stops an unbounded map under random-code probing.
const MAX_CACHED: usize = 8192;

/// `true` when `raw` has the exact shape of an overlay code: 16 characters,
/// each `0-9` or `a-f` (lowercase only). The single definition of the shape --
/// the migration's CHECK constraint, the route guards and the page script all
/// mirror it. Anything else can never name a community, so it is rejected
/// before any cache or database access.
pub fn is_valid_code(raw: &str) -> bool {
    raw.len() == CODE_LEN && raw.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// Why an [`OverlayCodeResolver`] lookup failed (not "the code is unknown" --
/// that is `Ok(None)`).
#[derive(Debug, Error)]
pub enum OverlayCodeError {
    /// The database read failed.
    #[error("overlay code lookup failed: {0}")]
    Db(#[from] DbErr),
}

/// Maps an overlay code to the community it names.
#[async_trait]
pub trait OverlayCodeResolver: Send + Sync {
    /// The community id `code` names, `Ok(None)` for a malformed or unknown
    /// code (a client error, never a fault), or an error if the lookup itself
    /// failed.
    async fn resolve(&self, code: &str) -> Result<Option<i64>, OverlayCodeError>;
}

/// One cached lookup result: `None` records "no such code".
struct CacheEntry {
    stored_at: Instant,
    community_id: Option<i64>,
}

/// [`OverlayCodeResolver`] over `communities`, with the TTL cache described in
/// the module docs.
pub struct SeaOrmOverlayCodeResolver {
    db: DatabaseConnection,
    found_ttl: Duration,
    absent_ttl: Duration,
    metrics: Option<OverlayCodeMetrics>,
    cache: Mutex<HashMap<String, CacheEntry>>,
}

impl SeaOrmOverlayCodeResolver {
    /// A resolver with the production [`CODE_TTL`] / [`ABSENT_TTL`].
    pub fn new(db: DatabaseConnection) -> Self {
        Self::with_ttl(db, CODE_TTL)
    }

    /// A resolver caching found mappings for `found_ttl` (and absent ones for
    /// the shorter of that and [`ABSENT_TTL`]). `Duration::ZERO` disables
    /// caching entirely -- every lookup reads the database.
    pub fn with_ttl(db: DatabaseConnection, found_ttl: Duration) -> Self {
        Self {
            db,
            found_ttl,
            absent_ttl: found_ttl.min(ABSENT_TTL),
            metrics: None,
            cache: Mutex::new(HashMap::new()),
        }
    }

    /// Attaches metric handles (lookup outcomes and database latency).
    #[must_use]
    pub fn with_metrics(mut self, metrics: OverlayCodeMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    fn count(&self, outcome: &str) {
        if let Some(metrics) = &self.metrics {
            metrics.lookups_total.with_label_values(&[outcome]).inc();
        }
    }

    /// Locks the cache, recovering from a poisoned lock: the map holds only
    /// plain owned values, so a panic elsewhere cannot leave it in a state that
    /// is unsafe to keep using.
    fn lock_cache(&self) -> std::sync::MutexGuard<'_, HashMap<String, CacheEntry>> {
        match self.cache.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    fn ttl_for(&self, community_id: Option<i64>) -> Duration {
        if community_id.is_some() {
            self.found_ttl
        } else {
            self.absent_ttl
        }
    }

    /// `Some(result)` when `code` has a live cached entry.
    fn cached(&self, code: &str) -> Option<Option<i64>> {
        let cache = self.lock_cache();
        let entry = cache.get(code)?;
        (entry.stored_at.elapsed() < self.ttl_for(entry.community_id)).then_some(entry.community_id)
    }

    fn remember(&self, code: &str, community_id: Option<i64>) {
        let mut cache = self.lock_cache();
        if cache.len() >= MAX_CACHED {
            // Shed expired entries and every absent one first (the cheap-to-
            // recreate, probe-controlled kind) ...
            cache.retain(|_, entry| {
                entry.community_id.is_some()
                    && entry.stored_at.elapsed() < self.ttl_for(entry.community_id)
            });
            // ... and only if the real mappings alone fill it, start over.
            if cache.len() >= MAX_CACHED {
                cache.clear();
            }
        }
        cache.insert(
            code.to_string(),
            CacheEntry {
                stored_at: Instant::now(),
                community_id,
            },
        );
    }
}

#[async_trait]
impl OverlayCodeResolver for SeaOrmOverlayCodeResolver {
    #[tracing::instrument(name = "overlay.code.resolve", skip_all)]
    async fn resolve(&self, code: &str) -> Result<Option<i64>, OverlayCodeError> {
        if !is_valid_code(code) {
            self.count("malformed");
            tracing::debug!("overlay code rejected: not 16 lowercase hex characters");
            return Ok(None);
        }
        if let Some(cached) = self.cached(code) {
            self.count("cache_hit");
            tracing::debug!(found = cached.is_some(), "overlay code served from cache");
            return Ok(cached);
        }

        let started = Instant::now();
        let row = CommunityOverlayCode::find()
            .filter(Column::OverlayCode.eq(code))
            .one(&self.db)
            .await;
        if let Some(metrics) = &self.metrics {
            metrics
                .lookup_duration_seconds
                .observe(started.elapsed().as_secs_f64());
        }
        let row = match row {
            Ok(row) => row,
            Err(err) => {
                self.count("db_error");
                tracing::error!(error = %err, "overlay code lookup failed");
                return Err(OverlayCodeError::Db(err));
            }
        };

        let community_id = row.map(|model| i64::from(model.id));
        self.count(if community_id.is_some() {
            "db_found"
        } else {
            "db_absent"
        });
        tracing::debug!(found = community_id.is_some(), "overlay code looked up");
        self.remember(code, community_id);
        Ok(community_id)
    }
}

/// An [`OverlayCodeResolver`] answering from a fixed table -- the seam tests and
/// local harnesses inject instead of standing up Postgres. Not used by the
/// running service.
#[derive(Default)]
pub struct StaticOverlayCodes {
    codes: HashMap<String, i64>,
    failing: bool,
}

impl StaticOverlayCodes {
    /// Resolves exactly the given `(code, community_id)` pairs; every other
    /// code is unknown.
    pub fn new<C: Into<String>>(entries: impl IntoIterator<Item = (C, i64)>) -> Self {
        Self {
            codes: entries
                .into_iter()
                .map(|(code, id)| (code.into(), id))
                .collect(),
            failing: false,
        }
    }

    /// Every lookup of a well-formed code fails like a database outage.
    pub fn failing() -> Self {
        Self {
            codes: HashMap::new(),
            failing: true,
        }
    }
}

#[async_trait]
impl OverlayCodeResolver for StaticOverlayCodes {
    async fn resolve(&self, code: &str) -> Result<Option<i64>, OverlayCodeError> {
        if !is_valid_code(code) {
            return Ok(None);
        }
        if self.failing {
            return Err(OverlayCodeError::Db(DbErr::Custom(
                "static resolver configured to fail".to_string(),
            )));
        }
        Ok(self.codes.get(code).copied())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::entities::community_overlay_code::Model;
    use crate::telemetry::register_overlay_code_metrics;
    use sea_orm::{DatabaseBackend, MockDatabase};

    const CODE: &str = "a1b2c3d4e5f60718";
    const OTHER: &str = "0123456789abcdef";

    fn row(id: i32, code: &str) -> Model {
        Model {
            id,
            overlay_code: code.to_string(),
        }
    }

    fn resolver_with(results: Vec<Vec<Model>>) -> SeaOrmOverlayCodeResolver {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results(results)
            .into_connection();
        SeaOrmOverlayCodeResolver::new(db)
    }

    #[test]
    fn only_sixteen_lowercase_hex_characters_are_a_code() {
        assert!(is_valid_code(CODE));
        assert!(is_valid_code("0000000000000000"));
        assert!(is_valid_code("ffffffffffffffff"));
        for bad in [
            "",
            "42",
            "a1b2c3d4e5f6071",       // 15
            "a1b2c3d4e5f607180",     // 17
            "A1B2C3D4E5F60718",      // uppercase
            "a1b2c3d4e5f6071g",      // non-hex
            "a1b2c3d4e5f6071-",      // punctuation
            " a1b2c3d4e5f6071",      // whitespace
            "a1b2c3d4e5f6071\u{e9}", // multi-byte char
            "health",
            "readyz",
            "overlay",
        ] {
            assert!(!is_valid_code(bad), "{bad:?} must not be a code");
        }
    }

    #[test]
    fn a_route_word_can_never_be_a_code() {
        // The root-level `/{overlay_code}/...` routes sit beside `/health`,
        // `/readyz`, `/overlay/...` and `/ws/...`: none of those first
        // segments is 16 hex characters, so they can never collide.
        for root in ["health", "readyz", "overlay", "ws", "metrics"] {
            assert!(!is_valid_code(root));
        }
    }

    #[tokio::test]
    async fn resolves_a_known_code_to_its_real_community_id() {
        let resolver = resolver_with(vec![vec![row(42, CODE)]]);
        assert_eq!(resolver.resolve(CODE).await.unwrap(), Some(42));
    }

    #[tokio::test]
    async fn an_unknown_code_is_none_not_an_error() {
        let resolver = resolver_with(vec![vec![]]);
        assert_eq!(resolver.resolve(OTHER).await.unwrap(), None);
    }

    #[tokio::test]
    async fn a_malformed_code_is_none_without_touching_the_database() {
        // Nothing queued: any query would error.
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let resolver = SeaOrmOverlayCodeResolver::new(db);
        for bad in ["42", "", "A1B2C3D4E5F60718", "../../etc/passwd"] {
            assert_eq!(resolver.resolve(bad).await.unwrap(), None, "{bad:?}");
        }
    }

    #[tokio::test]
    async fn a_database_error_is_surfaced_and_never_cached() {
        // First lookup: the mock has nothing queued, so the query errors.
        // Second lookup: a result is queued -- it is reached, proving the
        // error was not cached.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([DbErr::Custom("boom".to_string())])
            .append_query_results([vec![row(42, CODE)]])
            .into_connection();
        let resolver = SeaOrmOverlayCodeResolver::new(db);
        let err = resolver.resolve(CODE).await.unwrap_err();
        assert!(matches!(err, OverlayCodeError::Db(_)), "{err}");
        assert_eq!(resolver.resolve(CODE).await.unwrap(), Some(42));
    }

    #[tokio::test]
    async fn the_second_lookup_is_served_from_the_cache() {
        // One result queued: a second database read would fail.
        let resolver = resolver_with(vec![vec![row(42, CODE)]]);
        assert_eq!(resolver.resolve(CODE).await.unwrap(), Some(42));
        assert_eq!(resolver.resolve(CODE).await.unwrap(), Some(42));
    }

    #[tokio::test]
    async fn an_absent_code_is_cached_too_so_probing_does_not_hammer_the_database() {
        let resolver = resolver_with(vec![vec![]]);
        assert_eq!(resolver.resolve(OTHER).await.unwrap(), None);
        assert_eq!(resolver.resolve(OTHER).await.unwrap(), None);
    }

    #[tokio::test]
    async fn an_expired_entry_is_re_read_so_a_rotated_code_stops_resolving() {
        // The code names community 42 first, then (after "rotation") nothing.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![row(42, CODE)], vec![]])
            .into_connection();
        let resolver = SeaOrmOverlayCodeResolver::with_ttl(db, Duration::ZERO);
        assert_eq!(resolver.resolve(CODE).await.unwrap(), Some(42));
        assert_eq!(resolver.resolve(CODE).await.unwrap(), None);
    }

    #[test]
    fn the_absent_ttl_never_exceeds_the_configured_ttl_or_the_cap() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let long = SeaOrmOverlayCodeResolver::with_ttl(db, Duration::from_secs(300));
        assert_eq!(long.found_ttl, Duration::from_secs(300));
        assert_eq!(long.absent_ttl, ABSENT_TTL);
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let short = SeaOrmOverlayCodeResolver::with_ttl(db, Duration::from_secs(1));
        assert_eq!(short.absent_ttl, Duration::from_secs(1));
    }

    #[test]
    fn the_cache_is_bounded_and_probing_cannot_evict_real_mappings() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let resolver = SeaOrmOverlayCodeResolver::new(db);
        resolver.remember(CODE, Some(42));
        // A scanner floods the cache with absent codes.
        for n in 0..(MAX_CACHED as u64 * 2) {
            resolver.remember(&format!("{n:016x}"), None);
        }
        assert!(resolver.lock_cache().len() <= MAX_CACHED);
        assert_eq!(
            resolver.cached(CODE),
            Some(Some(42)),
            "the real mapping survived the flood"
        );
    }

    #[test]
    fn a_cache_full_of_real_mappings_is_cleared_rather_than_grown() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let resolver = SeaOrmOverlayCodeResolver::new(db);
        for n in 0..=(MAX_CACHED as u64) {
            resolver.remember(&format!("{n:016x}"), Some(n as i64));
        }
        assert!(resolver.lock_cache().len() <= MAX_CACHED);
        // The newest insert survives the overflow clear.
        let newest = format!("{:016x}", MAX_CACHED as u64);
        assert_eq!(resolver.cached(&newest), Some(Some(MAX_CACHED as i64)));
    }

    #[test]
    fn a_poisoned_cache_lock_is_recovered() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let resolver = std::sync::Arc::new(SeaOrmOverlayCodeResolver::new(db));
        let poisoner = resolver.clone();
        let _ = std::thread::spawn(move || {
            let _guard = poisoner.cache.lock().unwrap();
            panic!("poison the cache lock");
        })
        .join();
        assert!(resolver.cache.is_poisoned());
        assert!(resolver.cached(CODE).is_none());
        resolver.remember(CODE, Some(7));
        assert_eq!(resolver.cached(CODE), Some(Some(7)));
    }

    #[tokio::test]
    async fn lookups_are_counted_by_outcome_and_database_latency_is_observed() {
        let registry = prometheus::Registry::new();
        let metrics = register_overlay_code_metrics(&registry);
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![row(42, CODE)], vec![]])
            .append_query_errors([DbErr::Custom("boom".to_string())])
            .into_connection();
        let resolver = SeaOrmOverlayCodeResolver::new(db).with_metrics(metrics.clone());

        resolver.resolve("nope").await.unwrap(); // malformed
        resolver.resolve(CODE).await.unwrap(); // db_found
        resolver.resolve(CODE).await.unwrap(); // cache_hit
        resolver.resolve(OTHER).await.unwrap(); // db_absent
        resolver.resolve("fedcba9876543210").await.unwrap_err(); // db_error

        let count = |outcome: &str| metrics.lookups_total.with_label_values(&[outcome]).get();
        assert_eq!(count("malformed"), 1);
        assert_eq!(count("db_found"), 1);
        assert_eq!(count("cache_hit"), 1);
        assert_eq!(count("db_absent"), 1);
        assert_eq!(count("db_error"), 1);
        assert_eq!(
            metrics.lookup_duration_seconds.get_sample_count(),
            3,
            "only the three database reads are timed"
        );
    }

    #[tokio::test]
    async fn the_static_resolver_answers_a_fixed_table() {
        let codes = StaticOverlayCodes::new([(CODE, 42), (OTHER, 7)]);
        assert_eq!(codes.resolve(CODE).await.unwrap(), Some(42));
        assert_eq!(codes.resolve(OTHER).await.unwrap(), Some(7));
        assert_eq!(codes.resolve("ffffffffffffffff").await.unwrap(), None);
        assert_eq!(codes.resolve("42").await.unwrap(), None);
        assert_eq!(
            StaticOverlayCodes::default().resolve(CODE).await.unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn the_failing_static_resolver_fails_well_formed_codes_only() {
        let broken = StaticOverlayCodes::failing();
        assert!(matches!(
            broken.resolve(CODE).await,
            Err(OverlayCodeError::Db(_))
        ));
        assert_eq!(broken.resolve("42").await.unwrap(), None);
    }
}
