//! Output storage. Layout follows the generic bundle-object-storage design: a
//! single bundle bucket, the MEDIA HARNESS prefix, every object TAGGED
//! `app-bundle:<requesting bundle id>` so only that bundle can resolve it
//! (tag-checked, host-mediated). This module is the thin stand-in until the
//! generic storage capability publishes its host API; switching is a trait impl.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use object_store::aws::{AmazonS3, AmazonS3Builder};
use object_store::path::Path as ObjectPath;
use object_store::signer::Signer;
use object_store::{
    Attribute, AttributeValue, Attributes, GetOptions, ObjectStore, PutOptions, PutPayload, TagSet,
};

use crate::types::Handle;

/// The media-harness prefix inside the bundle bucket.
pub const MEDIA_PREFIX: &str = "media-harness";
/// Tag/metadata key carrying the owning bundle id.
pub const OWNER_TAG_KEY: &str = "app-bundle";

/// Object key for a handle: `media-harness/{tenant}/{community}/{job}.{ext}`
/// (numeric ids, never slug/name).
pub fn object_key(h: &Handle) -> String {
    format!(
        "{MEDIA_PREFIX}/{}/{}/{}.{}",
        h.tenant_id, h.community_id, h.job_id, h.ext
    )
}

/// The tag as documented: `app-bundle:<bundle id>`.
pub fn owner_tag(app_id: &str) -> String {
    format!("{OWNER_TAG_KEY}:{app_id}")
}

/// Storage failures.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// Misconfiguration.
    #[error("store config: {0}")]
    Config(String),
    /// Backend I/O failure (no object bytes or keys in the message).
    #[error("store backend: {0}")]
    Backend(String),
}

/// The storage seam.
#[async_trait]
pub trait MediaStore: Send + Sync {
    /// Writes `bytes` under `key`, tagged for `owner_app`.
    async fn put(
        &self,
        key: &str,
        bytes: Bytes,
        content_type: &str,
        owner_app: &str,
    ) -> Result<(), StoreError>;
    /// The bundle id tagged on `key`, or `None` when the object does not exist.
    async fn owner(&self, key: &str) -> Result<Option<String>, StoreError>;
    /// Short-TTL presigned GET URL for `key`.
    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StoreError>;
}

/// Production store: `object_store`'s S3 client against the SeaweedFS gateway
/// (same builder settings as `svc_presentation`'s image store).
#[derive(Clone, Debug)]
pub struct ObjectStoreMediaStore {
    client: Arc<AmazonS3>,
}

impl ObjectStoreMediaStore {
    /// Builds the client; never makes a network call.
    pub fn new(
        endpoint: &str,
        bucket: &str,
        region: &str,
        access_key_id: &str,
        secret_access_key: &str,
    ) -> Result<Self, StoreError> {
        let client = AmazonS3Builder::new()
            .with_endpoint(endpoint)
            .with_bucket_name(bucket)
            .with_region(region)
            .with_access_key_id(access_key_id)
            .with_secret_access_key(secret_access_key)
            .with_allow_http(endpoint.starts_with("http://"))
            // SeaweedFS rejects virtual-hosted-style addressing.
            .with_virtual_hosted_style_request(false)
            .build()
            .map_err(|e| StoreError::Config(e.to_string()))?;
        Ok(Self {
            client: Arc::new(client),
        })
    }
}

#[async_trait]
impl MediaStore for ObjectStoreMediaStore {
    async fn put(
        &self,
        key: &str,
        bytes: Bytes,
        content_type: &str,
        owner_app: &str,
    ) -> Result<(), StoreError> {
        let mut tags = TagSet::default();
        tags.push(OWNER_TAG_KEY, owner_app);
        let mut attrs = Attributes::new();
        attrs.insert(
            Attribute::ContentType,
            AttributeValue::from(content_type.to_string()),
        );
        // Metadata mirror of the tag: object_store cannot read tags back, and the
        // ownership check below must be against what is actually on the object.
        attrs.insert(
            Attribute::Metadata(OWNER_TAG_KEY.into()),
            AttributeValue::from(owner_app.to_string()),
        );
        let opts = PutOptions {
            tags,
            attributes: attrs,
            ..PutOptions::default()
        };
        self.client
            .put_opts(&ObjectPath::from(key), PutPayload::from(bytes), opts)
            .await
            .map_err(|e| {
                StoreError::Backend(format!(
                    "put failed: {}",
                    e.to_string().chars().take(120).collect::<String>()
                ))
            })?;
        Ok(())
    }

    async fn owner(&self, key: &str) -> Result<Option<String>, StoreError> {
        let opts = GetOptions {
            head: true,
            ..GetOptions::default()
        };
        match self.client.get_opts(&ObjectPath::from(key), opts).await {
            Ok(res) => Ok(res
                .attributes
                .get(&Attribute::Metadata(OWNER_TAG_KEY.into()))
                .map(|v| v.as_ref().to_string())),
            Err(object_store::Error::NotFound { .. }) => Ok(None),
            Err(e) => Err(StoreError::Backend(format!(
                "head failed: {}",
                e.to_string().chars().take(120).collect::<String>()
            ))),
        }
    }

    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StoreError> {
        self.client
            .signed_url(http::Method::GET, &ObjectPath::from(key), ttl)
            .await
            .map(|u| u.to_string())
            .map_err(|e| StoreError::Backend(format!("sign failed: {e}")))
    }
}

/// In-memory store for tests (public so downstream stage tests can use it).
#[derive(Default)]
pub struct InMemoryStore {
    objects: std::sync::Mutex<std::collections::HashMap<String, (Bytes, String, String)>>,
}

impl InMemoryStore {
    /// Number of stored objects.
    pub fn len(&self) -> usize {
        match self.objects.lock() {
            Ok(g) => g.len(),
            Err(p) => p.into_inner().len(),
        }
    }
    /// `true` when empty.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Stored `(bytes, content_type, owner)` for `key`.
    pub fn get(&self, key: &str) -> Option<(Bytes, String, String)> {
        match self.objects.lock() {
            Ok(g) => g.get(key).cloned(),
            Err(p) => p.into_inner().get(key).cloned(),
        }
    }
}

#[async_trait]
impl MediaStore for InMemoryStore {
    async fn put(
        &self,
        key: &str,
        bytes: Bytes,
        content_type: &str,
        owner_app: &str,
    ) -> Result<(), StoreError> {
        let mut g = self
            .objects
            .lock()
            .map_err(|_| StoreError::Backend("poisoned".into()))?;
        g.insert(
            key.to_string(),
            (bytes, content_type.to_string(), owner_app.to_string()),
        );
        Ok(())
    }
    async fn owner(&self, key: &str) -> Result<Option<String>, StoreError> {
        let g = self
            .objects
            .lock()
            .map_err(|_| StoreError::Backend("poisoned".into()))?;
        Ok(g.get(key).map(|(_, _, o)| o.clone()))
    }
    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StoreError> {
        Ok(format!(
            "http://fake-store.invalid/{key}?ttl={}",
            ttl.as_secs()
        ))
    }
}
