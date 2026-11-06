//! Production backend: `object_store` `AmazonS3` against SeaweedFS (or any
//! S3-compatible endpoint). Tags ride as one user-metadata attribute
//! (`waddles-tags`), read back via HEAD; `set_tags` is read-modify-write
//! (object_store has no PutObjectTagging; bodies are capped at 25 MiB).

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use futures::StreamExt;
use object_store::aws::{AmazonS3, AmazonS3Builder};
use object_store::path::Path as ObjectPath;
use object_store::signer::Signer;
use object_store::{
    Attribute, AttributeValue, Attributes, GetOptions, ObjectStore, ObjectStoreExt, PutOptions,
    PutPayload,
};

use crate::backend::{ObjectBackend, ObjectMeta, StoredObject};
use crate::error::StorageError;

const TAGS_ATTR: &str = "waddles-tags";

/// Connection settings; credentials come from the host's own env/secret, never the guest.
pub struct S3Config {
    pub endpoint: String,
    pub bucket: String,
    pub region: String,
    pub access_key_id: String,
    pub secret_access_key: String,
}

#[derive(Clone)]
pub struct S3Backend {
    client: Arc<AmazonS3>,
}

fn be(e: impl std::fmt::Display) -> StorageError {
    StorageError::Backend(e.to_string())
}

fn attrs(content_type: &str, tags: &[String]) -> Attributes {
    let mut a = Attributes::new();
    a.insert(
        Attribute::ContentType,
        AttributeValue::from(content_type.to_string()),
    );
    a.insert(
        Attribute::Metadata(TAGS_ATTR.into()),
        AttributeValue::from(tags.join(",")),
    );
    a
}

fn meta_from(key: &str, size: u64, a: &Attributes) -> ObjectMeta {
    let content_type = a
        .get(&Attribute::ContentType)
        .map(|v| v.to_string())
        .unwrap_or_else(|| "application/octet-stream".to_string());
    let tags = a
        .get(&Attribute::Metadata(TAGS_ATTR.into()))
        .map(|v| {
            v.as_ref()
                .split(',')
                .filter(|t| !t.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    ObjectMeta {
        key: key.into(),
        size,
        content_type,
        tags,
    }
}

impl S3Backend {
    /// Builds the client; fails loudly on bad config (no network call).
    pub fn new(cfg: &S3Config) -> Result<Self, StorageError> {
        let client = AmazonS3Builder::new()
            .with_endpoint(&cfg.endpoint)
            .with_bucket_name(&cfg.bucket)
            .with_region(&cfg.region)
            .with_access_key_id(&cfg.access_key_id)
            .with_secret_access_key(&cfg.secret_access_key)
            .with_allow_http(cfg.endpoint.starts_with("http://"))
            .with_virtual_hosted_style_request(false)
            .build()
            .map_err(be)?;
        Ok(Self {
            client: Arc::new(client),
        })
    }
}

#[async_trait]
impl ObjectBackend for S3Backend {
    async fn put(
        &self,
        key: &str,
        body: Bytes,
        content_type: &str,
        tags: &[String],
    ) -> Result<(), StorageError> {
        let opts = PutOptions {
            attributes: attrs(content_type, tags),
            ..Default::default()
        };
        self.client
            .put_opts(&ObjectPath::from(key), PutPayload::from(body), opts)
            .await
            .map_err(be)?;
        Ok(())
    }

    async fn head(&self, key: &str) -> Result<Option<ObjectMeta>, StorageError> {
        match self
            .client
            .get_opts(
                &ObjectPath::from(key),
                GetOptions {
                    head: true,
                    ..Default::default()
                },
            )
            .await
        {
            Ok(r) => Ok(Some(meta_from(key, r.meta.size, &r.attributes))),
            Err(object_store::Error::NotFound { .. }) => Ok(None),
            Err(e) => Err(be(e)),
        }
    }

    async fn get(&self, key: &str) -> Result<Option<StoredObject>, StorageError> {
        match self.client.get(&ObjectPath::from(key)).await {
            Ok(r) => {
                let (size, attributes) = (r.meta.size, r.attributes.clone());
                let body = r.bytes().await.map_err(be)?;
                Ok(Some(StoredObject {
                    meta: meta_from(key, size, &attributes),
                    body,
                }))
            }
            Err(object_store::Error::NotFound { .. }) => Ok(None),
            Err(e) => Err(be(e)),
        }
    }

    async fn list(
        &self,
        prefix: &str,
        after: Option<&str>,
        max: usize,
    ) -> Result<Vec<(String, u64)>, StorageError> {
        let p = ObjectPath::from(prefix.trim_end_matches('/'));
        let mut stream = match after {
            Some(a) => self.client.list_with_offset(Some(&p), &ObjectPath::from(a)),
            None => self.client.list(Some(&p)),
        };
        let mut out = Vec::new();
        while out.len() < max {
            match stream.next().await {
                Some(Ok(m)) => out.push((m.location.to_string(), m.size)),
                Some(Err(e)) => return Err(be(e)),
                None => break,
            }
        }
        Ok(out)
    }

    async fn delete(&self, key: &str) -> Result<(), StorageError> {
        match self.client.delete(&ObjectPath::from(key)).await {
            Ok(()) | Err(object_store::Error::NotFound { .. }) => Ok(()),
            Err(e) => Err(be(e)),
        }
    }

    async fn set_tags(&self, key: &str, tags: &[String]) -> Result<bool, StorageError> {
        let Some(obj) = self.get(key).await? else {
            return Ok(false);
        };
        self.put(key, obj.body, &obj.meta.content_type, tags)
            .await?;
        Ok(true)
    }

    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StorageError> {
        let url = self
            .client
            .signed_url(http::Method::GET, &ObjectPath::from(key), ttl)
            .await
            .map_err(be)?;
        Ok(url.to_string())
    }
}
