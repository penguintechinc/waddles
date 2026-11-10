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
        let client_options = s3_client_options(endpoint).map_err(StoreError::Config)?;
        let client = AmazonS3Builder::new()
            .with_client_options(client_options)
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

/// Builds the `object_store` HTTP client options for an S3 endpoint.
///
/// For an `https://` endpoint with `S3_CA_FILE` set, that internal CA is the ONLY trust anchor
/// (system roots are disabled) and an unreadable/invalid CA is a hard error. With `S3_CA_FILE`
/// unset (an external S3 with a public CA) the default system roots verify the server -- either
/// way verification is mandatory: there is no unverified or plaintext fallback. `http://`
/// endpoints (local/ci only) get the default options.
fn s3_client_options(endpoint: &str) -> Result<object_store::ClientOptions, String> {
    s3_client_options_from(endpoint, std::env::var("S3_CA_FILE").ok().as_deref())
}

/// Testable core of [`s3_client_options`]: the CA path is passed in instead of read from env.
fn s3_client_options_from(
    endpoint: &str,
    ca_file: Option<&str>,
) -> Result<object_store::ClientOptions, String> {
    let options = object_store::ClientOptions::new();
    if !endpoint.starts_with("https://") {
        return Ok(options);
    }
    let Some(path) = ca_file.filter(|p| !p.trim().is_empty()) else {
        return Ok(options);
    };
    let pem = std::fs::read(path).map_err(|e| format!("reading S3_CA_FILE {path}: {e}"))?;
    // `Certificate::from_pem` accepts input with no certificate block at all; an empty trust store would then
    // fail every handshake with an opaque error, so reject it here with the real cause.
    if !String::from_utf8_lossy(&pem).contains("-----BEGIN CERTIFICATE-----") {
        return Err(format!(
            "S3_CA_FILE {path} is not a valid PEM certificate: no CERTIFICATE block"
        ));
    }
    let cert = object_store::Certificate::from_pem(&pem)
        .map_err(|e| format!("S3_CA_FILE {path} is not a valid PEM certificate: {e}"))?;
    Ok(options
        .with_no_system_certificates(true)
        .with_root_certificate(cert))
}

#[cfg(test)]
mod s3_tls_tests {
    use super::*;

    const TEST_CA_PEM: &str = "-----BEGIN CERTIFICATE-----\nMIIBkDCCATWgAwIBAgIUTm8SzjxcM8sufBhcYwaEwUP7pu4wCgYIKoZIzj0EAwIw\nHDEaMBgGA1UEAwwRd2FkZGxlYm90LXRlc3QtY2EwIBcNMjYxMTA4MTkzOTQzWhgP\nMjEyNjEwMTUxOTM5NDNaMBwxGjAYBgNVBAMMEXdhZGRsZWJvdC10ZXN0LWNhMFkw\nEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEINiEVycQtvYWvcnqYnZXDMxV4OVlhszX\ndLi46RWZep8NGLZLbynjzp7tlOKTzjgxKRF9ISK3KbxEOmf4XR4UiaNTMFEwHQYD\nVR0OBBYEFC9YUuNgzqrV9j7O7s5wLRg/2J7DMB8GA1UdIwQYMBaAFC9YUuNgzqrV\n9j7O7s5wLRg/2J7DMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDSQAwRgIh\nAKz0+cVk2S72g0JTT5Xl9PXcffuK1Z3d+80gimuneQxMAiEAusO6L1rpePtRmIfn\nq03s7OHqTPdWUc9ozyB0h8Y3bgI=\n-----END CERTIFICATE-----\n";

    #[test]
    fn https_without_ca_uses_system_roots_not_plaintext() {
        assert!(s3_client_options_from("https://s3.example.com", None).is_ok());
        assert!(s3_client_options_from("https://s3.example.com", Some("  ")).is_ok());
    }

    #[test]
    fn https_with_missing_file_fails_loud() {
        let err = s3_client_options_from("https://s3.internal:8333", Some("/nonexistent/ca.pem"))
            .unwrap_err();
        assert!(err.contains("reading S3_CA_FILE"), "{err}");
    }

    #[test]
    fn https_with_garbage_pem_fails_loud() {
        let dir = std::env::temp_dir().join(format!(
            "s3ca-bad-{}-{}",
            module_path!().len(),
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("ca.pem");
        std::fs::write(&p, b"not a certificate").unwrap();
        let err = s3_client_options_from("https://s3.internal:8333", p.to_str()).unwrap_err();
        assert!(err.contains("not a valid PEM"), "{err}");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn https_with_valid_ca_pins_it() {
        let dir = std::env::temp_dir().join(format!(
            "s3ca-ok-{}-{}",
            module_path!().len(),
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("ca.pem");
        std::fs::write(&p, TEST_CA_PEM).unwrap();
        assert!(s3_client_options_from("https://s3.internal:8333", p.to_str()).is_ok());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn http_endpoint_needs_no_ca() {
        assert!(s3_client_options_from("http://127.0.0.1:1", None).is_ok());
    }
}
