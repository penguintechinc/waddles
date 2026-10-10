//! P9: the `Surface::image` renderer. Resolves one `Surface::image` push
//! (`overlay_schema::OverlayPush`, whose `image_url` field carries an
//! opaque `asset_id` reference for this surface -- see `crate::images`'
//! module doc for why) into the signed, scoped, expiring URL + display
//! params an overlay client actually loads.
//!
//! This is a pure transform over the two trait seams
//! (`crate::images::asset_store::AssetStore`, `crate::images::store::
//! ImageStore`) -- no axum route is mounted here. P2/P3 (render/live-SSE
//! routes, not yet merged as of this change) call
//! [`render_image_push`] from their own `Surface::image` dispatch arm once
//! they exist, the same "extension point, not a stub" precedent
//! `crate::overlay::router`'s module doc already establishes for
//! `with_view_guard`/`with_push_guard`.

use std::time::{Duration, Instant};

use overlay_schema::{OverlayPush, PushKind};
use thiserror::Error;
use uuid::Uuid;

use crate::images::asset_store::{AssetStore, AssetStoreError};
use crate::images::store::{ImageStore, ImageStoreError};
use crate::telemetry::ImageMetrics;

#[derive(Debug, Error)]
pub enum RenderError {
    /// A `Clear` push carries no asset to resolve -- P2/P3's own dispatch
    /// must special-case `PushKind::Clear` before ever calling into a
    /// per-surface renderer (same precedent `overlay_schema::push`'s
    /// module doc already documents for `render.py`'s `on_message`
    /// handlers). Returned rather than silently resolving to "nothing",
    /// so a dispatch bug that forgets the `Clear` special case fails
    /// loudly instead of rendering a meaningless image frame.
    #[error("a Clear push carries no image asset to render")]
    ClearPush,
    /// `image_url` was empty or not a valid UUID -- this surface requires
    /// an asset reference, never a raw external URL.
    #[error("invalid image asset reference: {0}")]
    InvalidAssetReference(String),
    /// No asset row matched `(community_id, asset_id)` -- covers both "no
    /// such asset" and "that asset belongs to a different community"
    /// (never distinguished, see `AssetStore::find`'s own doc).
    #[error("image asset not found")]
    NotFound,
    #[error(transparent)]
    AssetStore(#[from] AssetStoreError),
    #[error(transparent)]
    ImageStore(#[from] ImageStoreError),
}

/// The resolved, client-ready output of one `Surface::image` push --
/// deliberately a svc-presentation-internal type, not a field bolted onto
/// `overlay_schema::OverlayPush` (that contract is merged and not modified
/// by this change). P2/P3 embed this into whatever wire frame they
/// eventually send down the live overlay channel.
#[derive(Debug, Clone, PartialEq)]
pub struct ImageSurfaceRender {
    pub image_url: url::Url,
    pub position_x: Option<i32>,
    pub position_y: Option<i32>,
    pub width: Option<i32>,
    pub height: Option<i32>,
    pub duration_ms: Option<i64>,
}

/// Resolves `push` (a `Surface::image` push, scoped to `community_id` --
/// the verified `overlay_auth::PushCredential`'s own community, never a
/// value read from the request body) into an [`ImageSurfaceRender`].
/// `ttl` is `IMAGE_SIGNED_URL_TTL_SECONDS` from config.
pub async fn render_image_push(
    push: &OverlayPush,
    community_id: i64,
    asset_store: &dyn AssetStore,
    image_store: &dyn ImageStore,
    ttl: Duration,
    metrics: &ImageMetrics,
) -> Result<ImageSurfaceRender, RenderError> {
    if push.kind == Some(PushKind::Clear) {
        return Err(RenderError::ClearPush);
    }

    let asset_id_raw = push
        .image_url
        .as_deref()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| RenderError::InvalidAssetReference("image_url is empty".to_string()))?;
    let asset_id = Uuid::parse_str(asset_id_raw)
        .map_err(|err| RenderError::InvalidAssetReference(err.to_string()))?;

    let asset = asset_store
        .find(community_id, asset_id)
        .await?
        .ok_or(RenderError::NotFound)?;

    let sign_started = Instant::now();
    let signed = image_store.signed_get_url(&asset.object_key, ttl).await;
    metrics
        .sign_latency_seconds
        .observe(sign_started.elapsed().as_secs_f64());
    let image_url = signed?;

    Ok(ImageSurfaceRender {
        image_url,
        position_x: asset.position_x,
        position_y: asset.position_y,
        width: asset.width,
        height: asset.height,
        duration_ms: asset.duration_ms,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::images::asset_store::ImageAssetRecord;
    use async_trait::async_trait;
    use std::collections::HashMap;
    use std::sync::Mutex;

    struct FakeAssetStore(Mutex<HashMap<(i64, Uuid), ImageAssetRecord>>);

    #[async_trait]
    impl AssetStore for FakeAssetStore {
        async fn insert(
            &self,
            _asset: crate::images::asset_store::NewImageAsset,
        ) -> Result<(), AssetStoreError> {
            unimplemented!("not exercised by these tests")
        }

        async fn find(
            &self,
            community_id: i64,
            asset_id: Uuid,
        ) -> Result<Option<ImageAssetRecord>, AssetStoreError> {
            Ok(self
                .0
                .lock()
                .unwrap()
                .get(&(community_id, asset_id))
                .cloned())
        }
    }

    struct FakeImageStore;

    #[async_trait]
    impl ImageStore for FakeImageStore {
        async fn put(
            &self,
            _key: &str,
            _bytes: bytes::Bytes,
            _content_type: &str,
        ) -> Result<(), ImageStoreError> {
            unimplemented!("not exercised by these tests")
        }

        async fn signed_get_url(
            &self,
            key: &str,
            ttl: Duration,
        ) -> Result<url::Url, ImageStoreError> {
            Ok(url::Url::parse(&format!(
                "https://seaweedfs.example/{key}?X-Amz-Expires={}",
                ttl.as_secs()
            ))
            .unwrap())
        }
    }

    fn asset_record() -> ImageAssetRecord {
        ImageAssetRecord {
            object_key: "overlay-images/42/asset.png".to_string(),
            content_type: "image/png".to_string(),
            position_x: Some(10),
            position_y: Some(20),
            width: Some(200),
            height: Some(100),
            duration_ms: Some(5000),
        }
    }

    fn push_referencing(asset_id: Uuid) -> OverlayPush {
        OverlayPush {
            image_url: Some(asset_id.to_string()),
            ..Default::default()
        }
    }

    fn test_metrics() -> ImageMetrics {
        crate::telemetry::register_image_metrics(&prometheus::Registry::new())
    }

    #[tokio::test]
    async fn resolves_a_valid_push_into_a_signed_url_and_display_params() {
        let asset_id = Uuid::new_v4();
        let mut map = HashMap::new();
        map.insert((42_i64, asset_id), asset_record());
        let assets = FakeAssetStore(Mutex::new(map));
        let store = FakeImageStore;

        let rendered = render_image_push(
            &push_referencing(asset_id),
            42,
            &assets,
            &store,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .expect("a valid, scoped push must resolve");

        assert!(rendered
            .image_url
            .as_str()
            .starts_with("https://seaweedfs.example/overlay-images/42/asset.png"));
        assert_eq!(rendered.width, Some(200));
        assert_eq!(rendered.duration_ms, Some(5000));
    }

    #[tokio::test]
    async fn a_clear_push_is_rejected_rather_than_silently_resolved() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let assets = FakeAssetStore(Mutex::new(HashMap::new()));
        let err = render_image_push(
            &push,
            42,
            &assets,
            &FakeImageStore,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, RenderError::ClearPush));
    }

    #[tokio::test]
    async fn an_empty_image_url_is_rejected() {
        let push = OverlayPush::default();
        let assets = FakeAssetStore(Mutex::new(HashMap::new()));
        let err = render_image_push(
            &push,
            42,
            &assets,
            &FakeImageStore,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, RenderError::InvalidAssetReference(_)));
    }

    #[tokio::test]
    async fn a_non_uuid_image_url_is_rejected() {
        let push = OverlayPush {
            image_url: Some("not-a-uuid".to_string()),
            ..Default::default()
        };
        let assets = FakeAssetStore(Mutex::new(HashMap::new()));
        let err = render_image_push(
            &push,
            42,
            &assets,
            &FakeImageStore,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, RenderError::InvalidAssetReference(_)));
    }

    /// The tenant/community isolation proof: an asset that exists, but
    /// under a *different* `community_id`, must resolve `NotFound` -- never
    /// leak across communities even though the `asset_id` itself is valid
    /// and present in the store.
    #[tokio::test]
    async fn an_asset_scoped_to_a_different_community_is_not_found() {
        let asset_id = Uuid::new_v4();
        let mut map = HashMap::new();
        map.insert((91_i64, asset_id), asset_record());
        let assets = FakeAssetStore(Mutex::new(map));

        let err = render_image_push(
            &push_referencing(asset_id),
            42,
            &assets,
            &FakeImageStore,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, RenderError::NotFound));
    }

    #[tokio::test]
    async fn an_unknown_asset_id_is_not_found() {
        let assets = FakeAssetStore(Mutex::new(HashMap::new()));
        let err = render_image_push(
            &push_referencing(Uuid::new_v4()),
            42,
            &assets,
            &FakeImageStore,
            Duration::from_secs(300),
            &test_metrics(),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, RenderError::NotFound));
    }
}
