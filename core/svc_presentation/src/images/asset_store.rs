//! Metadata store for `overlay_images` (migration 101): insert at upload
//! time (P6), scoped lookup at render time (P9). Every read is filtered by
//! `(community_id, asset_id)` together -- never `asset_id` alone -- so a
//! community can never resolve another community's asset even if it
//! somehow learns/guesses a valid `asset_id` UUID. This is the tenant/
//! community isolation boundary RISK #3 depends on; `crate::images::store`
//! itself does no scoping at all.

use async_trait::async_trait;
use sea_orm::{ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter, Set};
use thiserror::Error;
use uuid::Uuid;

use crate::db::entities::overlay_image::{ActiveModel, Column, Entity as OverlayImage, Model};

#[derive(Debug, Error)]
pub enum AssetStoreError {
    #[error("asset store query failed: {0}")]
    Query(String),
}

/// One uploaded image asset's metadata -- everything
/// [`crate::images::render::render_image_push`] needs to resolve a push
/// into a signed URL + display params, without re-reading the DB for each
/// field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageAssetRecord {
    pub object_key: String,
    pub content_type: String,
    pub position_x: Option<i32>,
    pub position_y: Option<i32>,
    pub width: Option<i32>,
    pub height: Option<i32>,
    pub duration_ms: Option<i64>,
}

impl From<Model> for ImageAssetRecord {
    fn from(model: Model) -> Self {
        Self {
            object_key: model.object_key,
            content_type: model.content_type,
            position_x: model.position_x,
            position_y: model.position_y,
            width: model.width,
            height: model.height,
            duration_ms: model.duration_ms,
        }
    }
}

/// Fields `crate::images::upload::upload_image` already validated/computed
/// before insert -- `community_id` comes from the verified
/// `overlay_auth::PushCredential`, never the request body/path
/// (`rules/security.md` Tenant Isolation).
#[derive(Debug, Clone)]
pub struct NewImageAsset {
    pub community_id: i64,
    pub asset_id: Uuid,
    pub object_key: String,
    pub content_type: String,
    pub size_bytes: i64,
    pub sha256: String,
    pub position_x: Option<i32>,
    pub position_y: Option<i32>,
    pub width: Option<i32>,
    pub height: Option<i32>,
    pub duration_ms: Option<i64>,
}

#[async_trait]
pub trait AssetStore: Send + Sync {
    async fn insert(&self, asset: NewImageAsset) -> Result<(), AssetStoreError>;

    /// Scoped lookup -- `None` for a wrong community as well as a wrong
    /// `asset_id`, never distinguishable from the caller's point of view
    /// (`crate::images::render` maps both to the same "not found" error,
    /// never leaking whether `asset_id` exists in some other community).
    async fn find(
        &self,
        community_id: i64,
        asset_id: Uuid,
    ) -> Result<Option<ImageAssetRecord>, AssetStoreError>;
}

/// Production [`AssetStore`]: a thin SeaORM wrapper around `overlay_images`.
#[derive(Clone)]
pub struct SeaOrmImageAssetStore {
    db: DatabaseConnection,
}

impl SeaOrmImageAssetStore {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

#[async_trait]
impl AssetStore for SeaOrmImageAssetStore {
    async fn insert(&self, asset: NewImageAsset) -> Result<(), AssetStoreError> {
        let active = ActiveModel {
            community_id: Set(asset.community_id),
            asset_id: Set(asset.asset_id),
            object_key: Set(asset.object_key),
            content_type: Set(asset.content_type),
            size_bytes: Set(asset.size_bytes),
            sha256: Set(asset.sha256),
            position_x: Set(asset.position_x),
            position_y: Set(asset.position_y),
            width: Set(asset.width),
            height: Set(asset.height),
            duration_ms: Set(asset.duration_ms),
            ..Default::default()
        };
        active
            .insert(&self.db)
            .await
            .map_err(|err| AssetStoreError::Query(err.to_string()))?;
        Ok(())
    }

    async fn find(
        &self,
        community_id: i64,
        asset_id: Uuid,
    ) -> Result<Option<ImageAssetRecord>, AssetStoreError> {
        let row = OverlayImage::find()
            .filter(Column::CommunityId.eq(community_id))
            .filter(Column::AssetId.eq(asset_id))
            .one(&self.db)
            .await
            .map_err(|err| AssetStoreError::Query(err.to_string()))?;
        Ok(row.map(ImageAssetRecord::from))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn sample_model(community_id: i64, asset_id: Uuid) -> Model {
        Model {
            id: 1,
            community_id,
            asset_id,
            object_key: "overlay-images/42/asset.png".to_string(),
            content_type: "image/png".to_string(),
            size_bytes: 1024,
            sha256: "deadbeef".to_string(),
            position_x: Some(10),
            position_y: Some(20),
            width: Some(200),
            height: Some(100),
            duration_ms: Some(5000),
        }
    }

    fn store_with(rows: Vec<Model>) -> SeaOrmImageAssetStore {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([rows])
            .into_connection();
        SeaOrmImageAssetStore::new(db)
    }

    #[tokio::test]
    async fn find_returns_the_matching_row_scoped_to_community_and_asset() {
        let asset_id = Uuid::new_v4();
        let store = store_with(vec![sample_model(42, asset_id)]);
        let record = store
            .find(42, asset_id)
            .await
            .expect("query succeeds")
            .expect("row present");
        assert_eq!(record.object_key, "overlay-images/42/asset.png");
        assert_eq!(record.width, Some(200));
        assert_eq!(record.duration_ms, Some(5000));
    }

    #[tokio::test]
    async fn find_returns_none_when_no_row_matches() {
        let store = store_with(vec![]);
        let record = store
            .find(42, Uuid::new_v4())
            .await
            .expect("query succeeds");
        assert!(record.is_none());
    }

    #[tokio::test]
    async fn insert_issues_a_real_insert_statement() {
        let asset_id = Uuid::new_v4();
        // SeaORM's Postgres backend implements `ActiveModelTrait::insert`
        // via `INSERT ... RETURNING` -- a *query*, not a bare exec, under
        // `MockDatabase` -- so the mocked response is a query result row
        // (the inserted row echoed back), matching what a real Postgres
        // `RETURNING *` would hand back, not `MockExecResult`.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![sample_model(42, asset_id)]])
            .into_connection();
        let store = SeaOrmImageAssetStore::new(db);
        store
            .insert(NewImageAsset {
                community_id: 42,
                asset_id,
                object_key: "overlay-images/42/asset.png".to_string(),
                content_type: "image/png".to_string(),
                size_bytes: 1024,
                sha256: "deadbeef".to_string(),
                position_x: None,
                position_y: None,
                width: None,
                height: None,
                duration_ms: None,
            })
            .await
            .expect("insert must succeed against a mocked RETURNING row");
    }
}
