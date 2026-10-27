//! Object-storage seam for overlay image assets: upload (PUT) and signed
//! GET URL generation, backed by `object_store`'s `AmazonS3` client against
//! SeaweedFS's S3-compatible gateway (same crate/version
//! `core/svc_streaming/src/egress/record.rs` already uses for its own
//! uploads against the identical cluster). A narrow trait
//! ([`ImageStore`]) rather than a bare `Arc<dyn object_store::ObjectStore>`
//! so tests exercise [`crate::images::upload`]/[`crate::images::render`]
//! without a live SeaweedFS/MinIO instance -- same "wrap the external
//! dependency behind a narrow seam" precedent `core/bundle_executor::
//! invoke::ComponentSource` and `core/svc_action::flags::FeatureFlag`
//! already set in this repo.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use object_store::aws::AmazonS3Builder;
use object_store::path::Path as ObjectPath;
use object_store::signer::Signer;
use object_store::{ObjectStoreExt, PutPayload};
use thiserror::Error;
use url::Url;

use crate::config::Config;

#[derive(Debug, Error)]
pub enum ImageStoreError {
    #[error("image bucket is not configured: {0}")]
    NotConfigured(&'static str),
    #[error("invalid image bucket configuration: {0}")]
    Config(String),
    #[error("image bucket PUT failed: {0}")]
    Put(String),
    #[error("image bucket presigned-URL signing failed: {0}")]
    Sign(String),
}

/// Upload + presigned-GET-URL seam for overlay image assets. Every method
/// is scoped to one already-computed `key` (`crate::images::upload`/
/// `crate::images::render` own the `{prefix}/{community_id}/{asset_id}.
/// {ext}` convention) -- this trait does no community-scoping itself, that
/// boundary is enforced one layer up by `AssetStore` (DB-row lookup scoped
/// to `community_id`) before a `key` ever reaches here.
#[async_trait]
pub trait ImageStore: Send + Sync {
    /// Uploads `bytes` to `key`, declaring `content_type`. At-rest
    /// encryption is the bucket's own default SSE-S3 policy (`docs/guides/
    /// seaweedfs-object-storage.md` Encryption at Rest -- the
    /// `seaweedfs-bucket-init` Helm hook applies `put-bucket-encryption` as
    /// the bucket default), not a per-object header this call sets
    /// explicitly.
    async fn put(&self, key: &str, bytes: Bytes, content_type: &str)
        -> Result<(), ImageStoreError>;

    /// Issues a presigned, time-limited `GET` URL for `key` -- RISK #3:
    /// every overlay image is served this way, never via a flat/public
    /// path. `ttl` is the caller's (`crate::images::render`'s) configured
    /// `IMAGE_SIGNED_URL_TTL_SECONDS`.
    async fn signed_get_url(&self, key: &str, ttl: Duration) -> Result<Url, ImageStoreError>;
}

/// Production [`ImageStore`]: a single shared `AmazonS3` client pointed at
/// this cluster's SeaweedFS S3 gateway.
#[derive(Clone, Debug)]
pub struct ObjectStoreImageStore {
    client: Arc<object_store::aws::AmazonS3>,
}

impl ObjectStoreImageStore {
    /// Builds the store from `config`'s `IMAGE_BUCKET_*` CLI/env fields
    /// plus the `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY`
    /// secrets. Fails loudly (`ImageStoreError::NotConfigured`/`Config`)
    /// rather than silently -- a deployment that never enables
    /// `crate::flags::IMAGE_UPLOAD_FLAG` need not set these at all, so this
    /// is checked at first use (upload/render time), not at process
    /// startup (`rules/general.md` Red Flags: never crash the whole
    /// service over one optional feature's missing config).
    pub fn from_config(config: &Config) -> Result<Self, ImageStoreError> {
        let access_key_id = config
            .image_bucket_access_key_id
            .as_ref()
            .ok_or(ImageStoreError::NotConfigured("IMAGE_BUCKET_ACCESS_KEY_ID"))?;
        let secret_access_key = config.image_bucket_secret_access_key.as_ref().ok_or(
            ImageStoreError::NotConfigured("IMAGE_BUCKET_SECRET_ACCESS_KEY"),
        )?;
        let endpoint = &config.cli.image_bucket_endpoint;
        let allow_http = endpoint.starts_with("http://");
        let client = AmazonS3Builder::new()
            .with_endpoint(endpoint)
            .with_bucket_name(&config.cli.image_bucket_name)
            .with_region(&config.cli.image_bucket_region)
            .with_access_key_id(access_key_id.expose())
            .with_secret_access_key(secret_access_key.expose())
            .with_allow_http(allow_http)
            // SeaweedFS rejects virtual-hosted-style addressing --
            // `docs/guides/seaweedfs-object-storage.md`'s documented
            // `S3_FORCE_PATH_STYLE=true` requirement.
            .with_virtual_hosted_style_request(false)
            .build()
            .map_err(|err| ImageStoreError::Config(err.to_string()))?;
        Ok(Self {
            client: Arc::new(client),
        })
    }
}

#[async_trait]
impl ImageStore for ObjectStoreImageStore {
    async fn put(
        &self,
        key: &str,
        bytes: Bytes,
        _content_type: &str,
    ) -> Result<(), ImageStoreError> {
        let path = ObjectPath::from(key);
        self.client
            .put(&path, PutPayload::from(bytes))
            .await
            .map_err(|err| ImageStoreError::Put(err.to_string()))?;
        Ok(())
    }

    async fn signed_get_url(&self, key: &str, ttl: Duration) -> Result<Url, ImageStoreError> {
        let path = ObjectPath::from(key);
        self.client
            .signed_url(http::Method::GET, &path, ttl)
            .await
            .map_err(|err| ImageStoreError::Sign(err.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use clap::Parser;

    fn cli_with_bucket_env() -> CliConfig {
        CliConfig::parse_from([
            "svc-presentation",
            "--image-bucket-endpoint",
            "http://127.0.0.1:1",
            "--image-bucket-name",
            "waddlebot-assets",
            "--image-bucket-region",
            "us-east-1",
        ])
    }

    fn config_with(access_key: Option<&str>, secret_key: Option<&str>) -> Config {
        Config {
            cli: cli_with_bucket_env(),
            db_password: Secret::new("unused"),
            cache_password: None,
            image_bucket_access_key_id: access_key.map(Secret::new),
            image_bucket_secret_access_key: secret_key.map(Secret::new),
        }
    }

    #[test]
    fn from_config_fails_closed_without_an_access_key() {
        let err = ObjectStoreImageStore::from_config(&config_with(None, Some("secret")))
            .expect_err("missing access key must error, not silently build");
        assert!(matches!(err, ImageStoreError::NotConfigured(_)));
    }

    #[test]
    fn from_config_fails_closed_without_a_secret_key() {
        let err = ObjectStoreImageStore::from_config(&config_with(Some("key"), None))
            .expect_err("missing secret key must error, not silently build");
        assert!(matches!(err, ImageStoreError::NotConfigured(_)));
    }

    #[test]
    fn from_config_builds_when_both_credentials_are_present() {
        // `AmazonS3Builder::build()` only constructs the client (credential
        // provider + HTTP client) -- it never makes a network call, same
        // precedent `core/svc_streaming::egress::record::tests::
        // from_env_builds_a_sink_from_the_documented_env_vars` already
        // establishes for the identical builder.
        ObjectStoreImageStore::from_config(&config_with(Some("key"), Some("secret")))
            .expect("both credentials present must build successfully");
    }

    #[test]
    fn image_store_error_display_never_panics() {
        for err in [
            ImageStoreError::NotConfigured("IMAGE_BUCKET_ACCESS_KEY_ID"),
            ImageStoreError::Config("bad endpoint".to_string()),
            ImageStoreError::Put("connection refused".to_string()),
            ImageStoreError::Sign("signing failed".to_string()),
        ] {
            assert!(!err.to_string().is_empty());
        }
    }
}
