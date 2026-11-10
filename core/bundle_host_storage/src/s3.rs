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

fn meta_from(key: &str, size: u64, a: &Attributes) -> Result<ObjectMeta, StorageError> {
    // Every object is written with a content type (`attrs`); a missing one means
    // the stored metadata is corrupt, so fail loudly instead of guessing a default.
    let content_type = match a.get(&Attribute::ContentType) {
        Some(v) => v.to_string(),
        None => {
            tracing::error!(key, "stored object has no content type");
            return Err(StorageError::Backend(format!(
                "object {key} has no content type"
            )));
        }
    };
    // `attrs` always writes the tags attribute (possibly empty); absence means the
    // stored metadata is corrupt, so fail loudly rather than inventing "no tags".
    let tags: Vec<String> = match a.get(&Attribute::Metadata(TAGS_ATTR.into())) {
        Some(v) => v
            .as_ref()
            .split(',')
            .filter(|t| !t.is_empty())
            .map(str::to_string)
            .collect(),
        None => {
            tracing::error!(key, "stored object has no tags attribute");
            return Err(StorageError::Backend(format!(
                "object {key} has no tags attribute"
            )));
        }
    };
    Ok(ObjectMeta {
        key: key.into(),
        size,
        content_type,
        tags,
    })
}

impl S3Backend {
    /// Builds the client; fails loudly on bad config (no network call).
    pub fn new(cfg: &S3Config) -> Result<Self, StorageError> {
        let client_options = s3_client_options(&cfg.endpoint).map_err(be)?;
        let client = AmazonS3Builder::new()
            .with_client_options(client_options)
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
            Ok(r) => meta_from(key, r.meta.size, &r.attributes).map(Some),
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
                    meta: meta_from(key, size, &attributes)?,
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
