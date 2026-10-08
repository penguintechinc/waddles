//! Concrete [`overlay_auth::ViewCredentialStore`] backed by this service's
//! own `overlay_view_credentials` table (migration 100). This is the
//! implementation `overlay_auth`'s own module doc forecasts: "a future
//! svc-presentation-rust implements this (typically a thin SeaORM-backed
//! wrapper ...)".

use std::future::Future;
use std::pin::Pin;

use chrono::Utc;
use overlay_auth::{OverlayAuthError, ViewCredentialRecord, ViewCredentialStore};
use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::db::entities::overlay_view_credential::{Column, Entity as OverlayViewCredential};

/// Thin wrapper around a [`DatabaseConnection`] implementing
/// [`ViewCredentialStore`] against `overlay_view_credentials`.
#[derive(Clone)]
pub struct SeaOrmViewCredentialStore {
    db: DatabaseConnection,
}

impl SeaOrmViewCredentialStore {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

impl ViewCredentialStore for SeaOrmViewCredentialStore {
    fn find_by_community<'a>(
        &'a self,
        community_id: i64,
    ) -> Pin<
        Box<
            dyn Future<Output = Result<Option<ViewCredentialRecord>, OverlayAuthError>> + Send + 'a,
        >,
    > {
        Box::pin(async move {
            let row = OverlayViewCredential::find()
                .filter(Column::CommunityId.eq(community_id))
                .one(&self.db)
                .await
                .map_err(|err| OverlayAuthError::Store(err.to_string()))?;

            Ok(row.map(|model| ViewCredentialRecord {
                community_id: model.community_id,
                key_hash: model.key_hash,
                previous_key_hash: model.previous_key_hash,
                is_active: model.is_active,
                rotated_at: model.rotated_at.map(|dt| dt.with_timezone(&Utc)),
            }))
        })
    }

    /// Best-effort access-stat bump. Migration 100 does not (yet) carry a
    /// `last_accessed`/`access_count` column -- see that migration file's
    /// header for the legacy schema's equivalent columns -- so this is
    /// currently a documented no-op rather than a real write; honest about
    /// what it does today, not a silent stub masquerading as a real bump
    /// (`rules/general.md` Red Flags). A follow-up migration adding those
    /// columns upgrades this to a real `UPDATE` without changing the
    /// trait's call sites.
    fn record_access<'a>(
        &'a self,
        _community_id: i64,
    ) -> Pin<Box<dyn Future<Output = Result<(), OverlayAuthError>> + Send + 'a>> {
        Box::pin(async move { Ok(()) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::entities::overlay_view_credential::Model;
    use overlay_auth::hash_token;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn mock_store(rows: Vec<Model>) -> SeaOrmViewCredentialStore {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([rows])
            .into_connection();
        SeaOrmViewCredentialStore::new(db)
    }

    #[tokio::test]
    async fn find_by_community_maps_a_matching_row() {
        let store = mock_store(vec![Model {
            id: 1,
            community_id: 42,
            key_hash: hash_token("sometoken"),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]);
        let record = store
            .find_by_community(42)
            .await
            .expect("query succeeds")
            .expect("row present");
        assert_eq!(record.community_id, 42);
        assert!(record.is_active);
        assert!(record.previous_key_hash.is_none());
    }

    #[tokio::test]
    async fn find_by_community_returns_none_when_absent() {
        let store = mock_store(vec![]);
        let record = store.find_by_community(999).await.expect("query succeeds");
        assert!(record.is_none());
    }

    #[tokio::test]
    async fn record_access_is_always_ok() {
        let store = mock_store(vec![]);
        store
            .record_access(42)
            .await
            .expect("best-effort no-op never fails");
    }
}
