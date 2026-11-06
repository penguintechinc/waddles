//! Object-store seam. The host never sees S3 credentials-in-guest; the backend
//! owns the (host-side) client. `MemoryBackend` is a faithful in-memory
//! implementation used by unit tests; `s3::S3Backend` is production.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;

use crate::error::StorageError;

/// Object metadata as stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ObjectMeta {
    pub key: String,
    pub size: u64,
    pub content_type: String,
    pub tags: Vec<String>,
}

/// Metadata + body.
#[derive(Clone, Debug)]
pub struct StoredObject {
    pub meta: ObjectMeta,
    pub body: Bytes,
}

/// Keys are FULL bucket keys (already scoped by the host).
#[async_trait]
pub trait ObjectBackend: Send + Sync {
    async fn put(
        &self,
        key: &str,
        body: Bytes,
        content_type: &str,
        tags: &[String],
    ) -> Result<(), StorageError>;
    async fn head(&self, key: &str) -> Result<Option<ObjectMeta>, StorageError>;
    async fn get(&self, key: &str) -> Result<Option<StoredObject>, StorageError>;
    /// Keys + sizes under `prefix`, lexicographic, strictly after `after` (cursor), at most `max`.
    async fn list(
        &self,
        prefix: &str,
        after: Option<&str>,
        max: usize,
    ) -> Result<Vec<(String, u64)>, StorageError>;
    async fn delete(&self, key: &str) -> Result<(), StorageError>;
    /// Replaces the tag set of an existing object; `false` if absent.
    async fn set_tags(&self, key: &str, tags: &[String]) -> Result<bool, StorageError>;
    /// Host-signed, GET-only URL for exactly `key`.
    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StorageError>;
}

/// In-memory backend (tests).
#[derive(Default)]
pub struct MemoryBackend {
    objects: Mutex<BTreeMap<String, StoredObject>>,
}

impl MemoryBackend {
    pub fn new() -> Self {
        Self::default()
    }
    fn lock(
        &self,
    ) -> Result<std::sync::MutexGuard<'_, BTreeMap<String, StoredObject>>, StorageError> {
        self.objects
            .lock()
            .map_err(|_| StorageError::Backend("poisoned".into()))
    }
}

#[async_trait]
impl ObjectBackend for MemoryBackend {
    async fn put(
        &self,
        key: &str,
        body: Bytes,
        content_type: &str,
        tags: &[String],
    ) -> Result<(), StorageError> {
        let meta = ObjectMeta {
            key: key.into(),
            size: body.len() as u64,
            content_type: content_type.into(),
            tags: tags.to_vec(),
        };
        self.lock()?.insert(key.into(), StoredObject { meta, body });
        Ok(())
    }
    async fn head(&self, key: &str) -> Result<Option<ObjectMeta>, StorageError> {
        Ok(self.lock()?.get(key).map(|o| o.meta.clone()))
    }
    async fn get(&self, key: &str) -> Result<Option<StoredObject>, StorageError> {
        Ok(self.lock()?.get(key).cloned())
    }
    async fn list(
        &self,
        prefix: &str,
        after: Option<&str>,
        max: usize,
    ) -> Result<Vec<(String, u64)>, StorageError> {
        Ok(self
            .lock()?
            .iter()
            .filter(|(k, _)| k.starts_with(prefix) && after.is_none_or(|a| k.as_str() > a))
            .take(max)
            .map(|(k, o)| (k.clone(), o.meta.size))
            .collect())
    }
    async fn delete(&self, key: &str) -> Result<(), StorageError> {
        self.lock()?.remove(key);
        Ok(())
    }
    async fn set_tags(&self, key: &str, tags: &[String]) -> Result<bool, StorageError> {
        match self.lock()?.get_mut(key) {
            Some(o) => {
                o.meta.tags = tags.to_vec();
                Ok(true)
            }
            None => Ok(false),
        }
    }
    async fn presign_get(&self, key: &str, ttl: Duration) -> Result<String, StorageError> {
        Ok(format!("memory://signed/{key}?ttl={}", ttl.as_secs()))
    }
}
