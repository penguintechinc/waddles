//! Per-community render context the push route needs *besides* the push
//! itself: the tenant hub-api scopes display-name resolution by, and the
//! community's stored theme.
//!
//! A PUSH credential (`overlay_auth::PushCredential`) proves only
//! `community_id`; it carries no tenant claim. A community nests in exactly
//! one tenant (`communities.tenant_id`), so the tenant is *derived from the
//! verified community id* here -- never taken from the request body or path
//! (`rules/security.md` Tenant Isolation), and never guessed.
//!
//! [`CommunityContextStore`] is a trait (like [`crate::overlay::CaptionStore`])
//! so the push handler is tested against an in-memory fake;
//! [`SeaOrmCommunityContextStore`] is the production implementation, with a
//! short TTL cache because a chat-heavy overlay pushes many frames per second
//! per community and the tenant/theme change essentially never.
//!
//! Nothing here logs more than the community id: the tenant id and theme are
//! not PII, but there is no reason to repeat them in log lines either.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter};
use thiserror::Error;

use crate::db::entities::community::Entity as Community;
use crate::db::entities::presentation_config::{
    Column as ConfigColumn, Entity as PresentationConfig,
};
use crate::overlay::render::RenderTheme;

/// How long a looked-up context is served from memory before it is re-read.
/// Short enough that a theme edit in the designer shows up within a minute,
/// long enough to turn a busy chat overlay's per-message lookup into one DB
/// read per community per minute.
pub const CONTEXT_TTL: Duration = Duration::from_secs(60);

/// Upper bound on cached communities. A deployment has far fewer; the bound
/// only stops an unbounded map if community ids ever churn. On overflow the
/// whole cache is dropped (the next pushes simply re-read).
const MAX_CACHED: usize = 4096;

/// What the push route needs to know about a community.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommunityContext {
    /// The tenant hub-api scopes `ResolveDisplayNames` by -- always the
    /// community's own tenant, as a decimal string (hub-api accepts a numeric
    /// tenant id or a slug).
    pub tenant_id: String,
    /// The community's stored theme overrides (defaults when none stored).
    pub theme: RenderTheme,
}

/// Why a [`CommunityContextStore`] lookup failed.
#[derive(Debug, Error)]
pub enum CommunityContextError {
    /// No `communities` row for this id (the credential was minted for a
    /// community that no longer exists).
    #[error("community {0} not found")]
    NotFound(i64),
    /// The id does not fit the `communities.id` (INTEGER) column, so it can
    /// not name a real community.
    #[error("community id {0} is out of range")]
    OutOfRange(i64),
    /// The database read failed.
    #[error("community context lookup failed: {0}")]
    Db(#[from] DbErr),
}

/// Looks up a community's [`CommunityContext`] from its (credential-verified)
/// id.
#[async_trait]
pub trait CommunityContextStore: Send + Sync {
    /// Returns the context for `community_id`, or why it could not.
    async fn context(&self, community_id: i64) -> Result<CommunityContext, CommunityContextError>;
}

/// [`CommunityContextStore`] over `communities` + `presentation_config`,
/// with a TTL cache.
pub struct SeaOrmCommunityContextStore {
    db: DatabaseConnection,
    ttl: Duration,
    cache: Mutex<HashMap<i64, (Instant, CommunityContext)>>,
}

impl SeaOrmCommunityContextStore {
    /// Builds a store with the production [`CONTEXT_TTL`].
    pub fn new(db: DatabaseConnection) -> Self {
        Self::with_ttl(db, CONTEXT_TTL)
    }

    /// Builds a store with an explicit TTL (tests use a tiny or zero one to
    /// exercise expiry).
    pub fn with_ttl(db: DatabaseConnection, ttl: Duration) -> Self {
        Self {
            db,
            ttl,
            cache: Mutex::new(HashMap::new()),
        }
    }

    /// Locks the cache, recovering from a poisoned lock: the map holds only
    /// plain owned values, so a panic elsewhere cannot leave it in a state
    /// that is unsafe to keep using.
    fn lock_cache(&self) -> std::sync::MutexGuard<'_, HashMap<i64, (Instant, CommunityContext)>> {
        match self.cache.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    fn cached(&self, community_id: i64) -> Option<CommunityContext> {
        let cache = self.lock_cache();
        let (stored_at, ctx) = cache.get(&community_id)?;
        (stored_at.elapsed() < self.ttl).then(|| ctx.clone())
    }

    fn remember(&self, community_id: i64, ctx: &CommunityContext) {
        let mut cache = self.lock_cache();
        if cache.len() >= MAX_CACHED {
            cache.clear();
        }
        cache.insert(community_id, (Instant::now(), ctx.clone()));
    }
}

#[async_trait]
impl CommunityContextStore for SeaOrmCommunityContextStore {
    #[tracing::instrument(name = "overlay.community_ctx", skip(self))]
    async fn context(&self, community_id: i64) -> Result<CommunityContext, CommunityContextError> {
        if let Some(ctx) = self.cached(community_id) {
            tracing::debug!(community_id, "community context served from cache");
            return Ok(ctx);
        }
        let id = i32::try_from(community_id)
            .map_err(|_| CommunityContextError::OutOfRange(community_id))?;

        let community = Community::find_by_id(id)
            .one(&self.db)
            .await?
            .ok_or(CommunityContextError::NotFound(community_id))?;
        let config = PresentationConfig::find()
            .filter(ConfigColumn::CommunityId.eq(id))
            .one(&self.db)
            .await?;

        // No stored row is the documented "not yet themed" state, not a
        // failure: `RenderTheme::default()` resolves to the built-in look.
        let theme = match &config {
            Some(model) => RenderTheme::from(model),
            None => RenderTheme::default(),
        };
        let ctx = CommunityContext {
            tenant_id: community.tenant_id.to_string(),
            theme,
        };
        tracing::debug!(
            community_id,
            themed = config.is_some(),
            "community context loaded"
        );
        self.remember(community_id, &ctx);
        Ok(ctx)
    }
}

/// How a [`StaticCommunityContextStore`] fails when told to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StaticFailure {
    /// Answer [`CommunityContextError::NotFound`].
    NotFound,
    /// Answer [`CommunityContextError::Db`].
    Db,
}

/// A [`CommunityContextStore`] answering from a fixed value for every
/// community -- the seam tests and local harnesses inject instead of standing
/// up Postgres. Not used by the running service.
pub struct StaticCommunityContextStore {
    ctx: CommunityContext,
    failure: Option<StaticFailure>,
}

impl StaticCommunityContextStore {
    /// Every community resolves to `tenant_id` with the default theme.
    pub fn ok(tenant_id: impl Into<String>) -> Self {
        Self::with_theme(tenant_id, RenderTheme::default())
    }

    /// Every community resolves to `tenant_id` with `theme`.
    pub fn with_theme(tenant_id: impl Into<String>, theme: RenderTheme) -> Self {
        Self {
            ctx: CommunityContext {
                tenant_id: tenant_id.into(),
                theme,
            },
            failure: None,
        }
    }

    /// Every lookup fails with `failure`.
    pub fn failing(failure: StaticFailure) -> Self {
        Self {
            failure: Some(failure),
            ..Self::ok("0")
        }
    }
}

#[async_trait]
impl CommunityContextStore for StaticCommunityContextStore {
    async fn context(&self, community_id: i64) -> Result<CommunityContext, CommunityContextError> {
        match self.failure {
            None => Ok(self.ctx.clone()),
            Some(StaticFailure::NotFound) => Err(CommunityContextError::NotFound(community_id)),
            Some(StaticFailure::Db) => Err(CommunityContextError::Db(DbErr::Custom(
                "static store configured to fail".to_string(),
            ))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::entities::community::Model as CommunityModel;
    use crate::db::entities::presentation_config::Model as ConfigModel;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn community(id: i32, tenant_id: i32) -> CommunityModel {
        CommunityModel { id, tenant_id }
    }

    fn config(community_id: i32, color: Option<&str>) -> ConfigModel {
        ConfigModel {
            id: 1,
            community_id,
            theme: "dark".to_string(),
            primary_color: color.map(str::to_string),
            secondary_color: None,
            font_family: None,
            music_enabled: true,
            crawler_speed_seconds: 20,
            config: serde_json::json!({}),
        }
    }

    #[tokio::test]
    async fn the_static_store_answers_fixed_values_and_configured_failures() {
        let ok = StaticCommunityContextStore::ok("7");
        assert_eq!(ok.context(1).await.unwrap().tenant_id, "7");
        let themed = StaticCommunityContextStore::with_theme(
            "8",
            RenderTheme {
                primary_color: Some("#112233".to_string()),
                ..Default::default()
            },
        );
        assert_eq!(
            themed
                .context(1)
                .await
                .unwrap()
                .theme
                .primary_color
                .as_deref(),
            Some("#112233")
        );
        let missing = StaticCommunityContextStore::failing(StaticFailure::NotFound);
        assert!(matches!(
            missing.context(9).await,
            Err(CommunityContextError::NotFound(9))
        ));
        let broken = StaticCommunityContextStore::failing(StaticFailure::Db);
        assert!(matches!(
            broken.context(9).await,
            Err(CommunityContextError::Db(_))
        ));
    }

    #[tokio::test]
    async fn loads_the_tenant_and_the_stored_theme() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community(42, 7)]])
            .append_query_results([vec![config(42, Some("#abcdef"))]])
            .into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let ctx = store.context(42).await.unwrap();
        assert_eq!(ctx.tenant_id, "7");
        assert_eq!(ctx.theme.primary_color.as_deref(), Some("#abcdef"));
    }

    #[tokio::test]
    async fn a_community_without_a_stored_theme_gets_the_default_theme() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community(42, 7)]])
            .append_query_results([Vec::<ConfigModel>::new()])
            .into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let ctx = store.context(42).await.unwrap();
        assert_eq!(ctx.tenant_id, "7");
        assert_eq!(ctx.theme, RenderTheme::default());
    }

    #[tokio::test]
    async fn an_unknown_community_is_not_found() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<CommunityModel>::new()])
            .into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let err = store.context(42).await.unwrap_err();
        assert!(matches!(err, CommunityContextError::NotFound(42)), "{err}");
    }

    #[tokio::test]
    async fn an_id_outside_the_integer_column_is_rejected_without_a_query() {
        // No query results queued: a DB hit would error differently.
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let err = store.context(i64::from(i32::MAX) + 1).await.unwrap_err();
        assert!(matches!(err, CommunityContextError::OutOfRange(_)), "{err}");
    }

    #[tokio::test]
    async fn a_database_error_is_surfaced_not_defaulted() {
        // Nothing queued: the mock DB errors on the first query.
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let err = store.context(42).await.unwrap_err();
        assert!(matches!(err, CommunityContextError::Db(_)), "{err}");
    }

    #[tokio::test]
    async fn the_second_lookup_is_served_from_the_cache() {
        // Exactly one community + one config result queued: a second DB read
        // would fail, so the second Ok proves the cache.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community(42, 7)]])
            .append_query_results([Vec::<ConfigModel>::new()])
            .into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let first = store.context(42).await.unwrap();
        let second = store.context(42).await.unwrap();
        assert_eq!(first, second);
    }

    #[tokio::test]
    async fn an_expired_entry_is_re_read() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community(42, 7)]])
            .append_query_results([Vec::<ConfigModel>::new()])
            .append_query_results([vec![community(42, 8)]])
            .append_query_results([Vec::<ConfigModel>::new()])
            .into_connection();
        let store = SeaOrmCommunityContextStore::with_ttl(db, Duration::ZERO);
        assert_eq!(store.context(42).await.unwrap().tenant_id, "7");
        assert_eq!(store.context(42).await.unwrap().tenant_id, "8");
    }

    #[tokio::test]
    async fn the_cache_is_bounded() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let store = SeaOrmCommunityContextStore::new(db);
        let ctx = CommunityContext {
            tenant_id: "1".to_string(),
            theme: RenderTheme::default(),
        };
        for id in 0..=(MAX_CACHED as i64) {
            store.remember(id, &ctx);
        }
        assert!(store.lock_cache().len() <= MAX_CACHED);
        // The most recent insert survives the overflow clear.
        assert!(store.cached(MAX_CACHED as i64).is_some());
    }

    #[test]
    fn a_poisoned_cache_lock_is_recovered() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let store = std::sync::Arc::new(SeaOrmCommunityContextStore::new(db));
        let poisoner = store.clone();
        let _ = std::thread::spawn(move || {
            let _guard = poisoner.cache.lock().unwrap();
            panic!("poison the cache lock");
        })
        .join();
        assert!(store.cache.is_poisoned());
        // Still usable.
        assert!(store.cached(1).is_none());
        store.remember(
            1,
            &CommunityContext {
                tenant_id: "1".to_string(),
                theme: RenderTheme::default(),
            },
        );
        assert!(store.cached(1).is_some());
    }
}
