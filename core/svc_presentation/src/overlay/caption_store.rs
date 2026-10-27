//! Persistence seam for the caption overlay's short-lived history: the
//! `caption_events` table (migration 102) behind the websocket's
//! reconnect replay -- "send recent captions on connect" in the Python
//! module this ports.
//!
//! [`CaptionStore`] is a trait (like `crate::images::AssetStore`) so the
//! ingest handler and websocket are tested against an in-memory fake without
//! standing up Postgres; [`SeaOrmCaptionStore`] is the production
//! implementation. Every read and write is scoped by `community_id` taken
//! from the caller's *verified credential* -- never a request body or path
//! segment (`rules/security.md` Tenant Isolation).
//!
//! Only the tokenized author reference (`user_ref`) is persisted, never a
//! username or display name (`critical-rules.md` PII Tokenization) -- see
//! `crate::db::entities::caption_event`.
//!
//! [`spawn_retention_task`] is the purge the 007 migration's comment
//! promised ("auto-purged after 7 days") but never had a caller: without
//! it the table would grow without bound.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use sea_orm::{
    ActiveValue::{NotSet, Set},
    ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder, QuerySelect,
};
use thiserror::Error;
use uuid::Uuid;

use crate::db::entities::caption_event::{
    ActiveModel, Column, Entity as CaptionEvent, Model as CaptionEventModel,
};
use crate::flags::FeatureFlag;

/// How far back a (re)connecting overlay is replayed -- the Python module's
/// hard-coded `NOW() - INTERVAL '5 minutes'`.
pub const HISTORY_WINDOW: Duration = Duration::from_secs(5 * 60);
/// Maximum captions replayed on connect -- the Python module's `LIMIT 10`.
pub const HISTORY_LIMIT: u64 = 10;
/// How long a caption is retained -- migration 007's documented 7 days.
pub const RETENTION: Duration = Duration::from_secs(7 * 24 * 60 * 60);
/// How often the retention purge runs.
pub const PURGE_INTERVAL: Duration = Duration::from_secs(60 * 60);

/// Why a [`CaptionStore`] operation failed. Wraps the database error so a
/// caller can log its type and message; never carries caption content.
#[derive(Debug, Error)]
pub enum CaptionStoreError {
    #[error("caption store query failed: {0}")]
    Db(#[from] DbErr),
}

/// One caption to persist. `community_id` is the verified credential's
/// community; `user_ref` is the tokenized author UUID.
#[derive(Debug, Clone, PartialEq)]
pub struct NewCaptionEvent {
    pub community_id: i64,
    pub user_ref: Uuid,
    pub platform: String,
    pub original: String,
    pub translated: Option<String>,
    pub detected_lang: Option<String>,
    pub target_lang: Option<String>,
    pub confidence: Option<f64>,
}

/// One persisted caption as replayed to a reconnecting overlay. Has no
/// display name: only the tokenized `user_ref` is stored.
#[derive(Debug, Clone, PartialEq)]
pub struct StoredCaption {
    pub user_ref: Option<Uuid>,
    pub platform: String,
    pub original: String,
    pub translated: Option<String>,
    pub detected_lang: Option<String>,
    pub target_lang: Option<String>,
    pub confidence: Option<f64>,
    pub created_at: DateTime<Utc>,
}

impl From<CaptionEventModel> for StoredCaption {
    fn from(model: CaptionEventModel) -> Self {
        Self {
            user_ref: model.user_ref,
            platform: model.platform,
            original: model.original_message,
            translated: model.translated_message,
            detected_lang: model.detected_language,
            target_lang: model.target_language,
            confidence: model.confidence_score,
            created_at: model.created_at.with_timezone(&Utc),
        }
    }
}

/// Backing store for caption history.
#[async_trait]
pub trait CaptionStore: Send + Sync {
    /// Persists one caption.
    async fn insert(&self, event: NewCaptionEvent) -> Result<(), CaptionStoreError>;

    /// The newest `limit` captions for `community_id` created after
    /// `since`, returned oldest-first (the order they should be replayed in).
    async fn recent(
        &self,
        community_id: i64,
        since: DateTime<Utc>,
        limit: u64,
    ) -> Result<Vec<StoredCaption>, CaptionStoreError>;

    /// Deletes every caption (all communities) created before `cutoff`;
    /// returns how many rows were removed.
    async fn purge_older_than(&self, cutoff: DateTime<Utc>) -> Result<u64, CaptionStoreError>;
}

/// SeaORM-backed [`CaptionStore`] over `caption_events`.
#[derive(Clone)]
pub struct SeaOrmCaptionStore {
    db: DatabaseConnection,
}

impl SeaOrmCaptionStore {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

#[async_trait]
impl CaptionStore for SeaOrmCaptionStore {
    async fn insert(&self, event: NewCaptionEvent) -> Result<(), CaptionStoreError> {
        let active = ActiveModel {
            id: NotSet,
            community_id: Set(event.community_id),
            user_ref: Set(Some(event.user_ref)),
            platform: Set(event.platform),
            original_message: Set(event.original),
            translated_message: Set(event.translated),
            detected_language: Set(event.detected_lang),
            target_language: Set(event.target_lang),
            confidence_score: Set(event.confidence),
            // The column default (`NOW()`) stamps the row.
            created_at: NotSet,
        };
        // `exec_without_returning`: nothing here needs the generated row
        // back, so skip the `RETURNING` round trip.
        CaptionEvent::insert(active)
            .exec_without_returning(&self.db)
            .await?;
        Ok(())
    }

    async fn recent(
        &self,
        community_id: i64,
        since: DateTime<Utc>,
        limit: u64,
    ) -> Result<Vec<StoredCaption>, CaptionStoreError> {
        let mut rows = CaptionEvent::find()
            .filter(Column::CommunityId.eq(community_id))
            .filter(Column::CreatedAt.gt(since))
            .order_by_desc(Column::CreatedAt)
            .limit(limit)
            .all(&self.db)
            .await?;
        // Newest-first from the query (so LIMIT keeps the *latest* rows);
        // replay oldest-first.
        rows.reverse();
        Ok(rows.into_iter().map(StoredCaption::from).collect())
    }

    async fn purge_older_than(&self, cutoff: DateTime<Utc>) -> Result<u64, CaptionStoreError> {
        let result = CaptionEvent::delete_many()
            .filter(Column::CreatedAt.lt(cutoff))
            .exec(&self.db)
            .await?;
        Ok(result.rows_affected)
    }
}

/// Runs one retention pass: deletes captions older than `retention`.
/// Split out from [`spawn_retention_task`]'s loop so it is directly testable.
/// A failure is logged with its error type and message and returned -- the
/// loop keeps running (one failed purge must not stop the next), but the
/// failure is never swallowed silently.
pub async fn run_retention_once(
    store: &dyn CaptionStore,
    retention: Duration,
) -> Result<u64, CaptionStoreError> {
    // A retention too large to represent as a timestamp offset means
    // "keep everything" -- nothing is old enough to purge.
    let Some(cutoff) = chrono::Duration::from_std(retention)
        .ok()
        .and_then(|window| Utc::now().checked_sub_signed(window))
    else {
        tracing::warn!("caption retention window exceeds the representable range; skipping purge");
        return Ok(0);
    };
    match store.purge_older_than(cutoff).await {
        Ok(removed) => {
            tracing::debug!(removed, "caption retention purge complete");
            Ok(removed)
        }
        Err(err) => {
            tracing::error!(
                error = %err,
                error_debug = ?err,
                "caption retention purge failed"
            );
            Err(err)
        }
    }
}

/// Spawns the background task that purges expired captions every
/// `interval`. The first pass runs immediately so a restart after a long
/// outage trims the backlog without waiting a full interval. Abort the
/// returned handle on shutdown.
///
/// Each pass is gated on `flag` (the captions feature flag): while the
/// feature is OFF this service must not touch `caption_events` at all -- the
/// table still belongs to the Python module until the cutover -- so a pass
/// with the flag off is skipped, and re-evaluated at the next interval.
pub fn spawn_retention_task(
    store: Arc<dyn CaptionStore>,
    flag: Arc<dyn FeatureFlag>,
    retention: Duration,
    interval: Duration,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            if !flag.enabled().await {
                tracing::debug!("captions flag is off; skipping caption retention purge");
                continue;
            }
            // Outcome already logged inside `run_retention_once`.
            let _ = run_retention_once(store.as_ref(), retention).await;
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use sea_orm::{DatabaseBackend, MockDatabase, MockExecResult};
    use std::sync::Mutex;

    fn model(id: i64, original: &str, created_at: DateTime<Utc>) -> CaptionEventModel {
        CaptionEventModel {
            id,
            community_id: 42,
            user_ref: Some(Uuid::parse_str("11111111-1111-1111-1111-111111111111").unwrap()),
            platform: "twitch".to_string(),
            original_message: original.to_string(),
            translated_message: Some(format!("{original}-translated")),
            detected_language: Some("es".to_string()),
            target_language: Some("en".to_string()),
            confidence_score: Some(0.9),
            created_at: created_at.fixed_offset(),
        }
    }

    /// The SQL text of the single statement the mock connection saw.
    fn only_sql(store: &SeaOrmCaptionStore) -> String {
        let log = store.db.clone().into_transaction_log();
        assert_eq!(log.len(), 1, "expected exactly one statement");
        let statements = log[0].statements();
        assert_eq!(statements.len(), 1);
        statements[0].sql.clone()
    }

    fn new_event() -> NewCaptionEvent {
        NewCaptionEvent {
            community_id: 42,
            user_ref: Uuid::parse_str("11111111-1111-1111-1111-111111111111").unwrap(),
            platform: "twitch".to_string(),
            original: "hola".to_string(),
            translated: Some("hello".to_string()),
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence: Some(0.9),
        }
    }

    #[tokio::test]
    async fn insert_issues_one_scoped_insert_statement() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_exec_results([MockExecResult {
                last_insert_id: 1,
                rows_affected: 1,
            }])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        store.insert(new_event()).await.expect("insert succeeds");

        let sql = only_sql(&store);
        assert!(sql.contains("INSERT INTO \"caption_events\""), "{sql}");
        assert!(sql.contains("community_id"), "{sql}");
        assert!(sql.contains("user_ref"), "{sql}");
        // The row is stamped by the column default, and no username column
        // is ever written.
        assert!(!sql.contains("created_at"), "{sql}");
        assert!(!sql.contains("username"), "{sql}");
    }

    #[tokio::test]
    async fn insert_surfaces_a_database_error() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_exec_errors([DbErr::Custom("boom".to_string())])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        let err = store.insert(new_event()).await.unwrap_err();
        assert!(err.to_string().contains("boom"));
    }

    #[tokio::test]
    async fn recent_returns_rows_oldest_first_and_is_community_scoped() {
        let t0 = Utc.with_ymd_and_hms(2026, 10, 9, 12, 0, 0).unwrap();
        // The query is `ORDER BY created_at DESC` (newest first).
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                model(3, "third", t0 + chrono::Duration::seconds(2)),
                model(2, "second", t0 + chrono::Duration::seconds(1)),
                model(1, "first", t0),
            ]])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        let got = store
            .recent(42, t0 - chrono::Duration::minutes(5), HISTORY_LIMIT)
            .await
            .unwrap();
        let originals: Vec<_> = got.iter().map(|c| c.original.as_str()).collect();
        assert_eq!(originals, ["first", "second", "third"]);
        assert_eq!(got[0].translated.as_deref(), Some("first-translated"));
        assert_eq!(got[0].created_at, t0);

        let sql = only_sql(&store);
        assert!(sql.contains("\"community_id\" ="), "{sql}");
        assert!(
            sql.contains("ORDER BY \"caption_events\".\"created_at\" DESC"),
            "{sql}"
        );
        assert!(sql.contains("LIMIT"), "{sql}");
        // The select list never names the legacy PII column.
        assert!(!sql.contains("username"), "{sql}");
    }

    #[tokio::test]
    async fn recent_returns_empty_when_nothing_is_recent() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<CaptionEventModel>::new()])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        assert!(store
            .recent(42, Utc::now(), HISTORY_LIMIT)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn recent_surfaces_a_database_error() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([DbErr::Custom("down".to_string())])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        assert!(store.recent(42, Utc::now(), 10).await.is_err());
    }

    #[tokio::test]
    async fn purge_deletes_older_rows_and_reports_the_count() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_exec_results([MockExecResult {
                last_insert_id: 0,
                rows_affected: 7,
            }])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        let removed = store.purge_older_than(Utc::now()).await.unwrap();
        assert_eq!(removed, 7);
        let sql = only_sql(&store);
        assert!(sql.contains("DELETE FROM \"caption_events\""), "{sql}");
        assert!(sql.contains("\"created_at\" <"), "{sql}");
    }

    #[tokio::test]
    async fn purge_surfaces_a_database_error() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_exec_errors([DbErr::Custom("locked".to_string())])
            .into_connection();
        let store = SeaOrmCaptionStore::new(db);
        assert!(store.purge_older_than(Utc::now()).await.is_err());
    }

    /// Records purge cutoffs and answers with a canned result.
    #[derive(Default)]
    struct RecordingStore {
        cutoffs: Mutex<Vec<DateTime<Utc>>>,
        fail: bool,
    }

    #[async_trait]
    impl CaptionStore for RecordingStore {
        async fn insert(&self, _event: NewCaptionEvent) -> Result<(), CaptionStoreError> {
            unimplemented!("not used by retention tests")
        }
        async fn recent(
            &self,
            _community_id: i64,
            _since: DateTime<Utc>,
            _limit: u64,
        ) -> Result<Vec<StoredCaption>, CaptionStoreError> {
            unimplemented!("not used by retention tests")
        }
        async fn purge_older_than(&self, cutoff: DateTime<Utc>) -> Result<u64, CaptionStoreError> {
            self.cutoffs.lock().unwrap().push(cutoff);
            if self.fail {
                Err(CaptionStoreError::Db(DbErr::Custom("purge failed".into())))
            } else {
                Ok(3)
            }
        }
    }

    #[tokio::test]
    async fn run_retention_once_purges_everything_older_than_the_retention_window() {
        let store = RecordingStore::default();
        let before = Utc::now();
        let removed = run_retention_once(&store, RETENTION).await.unwrap();
        assert_eq!(removed, 3);
        let cutoff = store.cutoffs.lock().unwrap()[0];
        let expected = before - chrono::Duration::days(7);
        assert!((cutoff - expected).num_seconds().abs() < 5, "{cutoff}");
    }

    #[tokio::test]
    async fn run_retention_once_skips_an_unrepresentably_large_window() {
        let store = RecordingStore::default();
        let removed = run_retention_once(&store, Duration::MAX).await.unwrap();
        assert_eq!(removed, 0);
        assert!(store.cutoffs.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn run_retention_once_returns_the_failure_after_logging_it() {
        let store = RecordingStore {
            fail: true,
            ..Default::default()
        };
        let err = run_retention_once(&store, RETENTION).await.unwrap_err();
        assert!(err.to_string().contains("purge failed"));
    }

    #[tokio::test]
    async fn retention_task_purges_immediately_then_every_interval_and_survives_failures() {
        let store = Arc::new(RecordingStore {
            fail: true,
            ..Default::default()
        });
        let handle = spawn_retention_task(
            store.clone(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
            Duration::from_secs(60),
            Duration::from_millis(20),
        );
        // Immediate first pass, then one per interval -- and because every
        // pass fails, reaching a second and third proves the loop survives
        // a failed purge instead of exiting.
        tokio::time::sleep(Duration::from_millis(150)).await;
        assert!(
            store.cutoffs.lock().unwrap().len() >= 3,
            "expected at least three purge passes"
        );
        handle.abort();
        assert!(handle.await.unwrap_err().is_cancelled());
    }

    #[tokio::test]
    async fn retention_task_never_touches_the_table_while_the_flag_is_off() {
        let store = Arc::new(RecordingStore::default());
        let handle = spawn_retention_task(
            store.clone(),
            crate::flags::boxed(crate::flags::StaticFlag(false)),
            Duration::from_secs(60),
            Duration::from_millis(20),
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
        handle.abort();
        assert!(
            store.cutoffs.lock().unwrap().is_empty(),
            "a flag-off pass must not purge"
        );
    }
}
