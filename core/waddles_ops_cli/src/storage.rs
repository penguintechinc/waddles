//! S3 (SigV4) upload of the signed bundle sidecar.
//!
//! Uses the `object_store` crate's AWS backend -- the same S3 client the repo's
//! `svc_streaming`/`svc_presentation` already depend on (SigV4 signing is its job, nothing is
//! hand-rolled here). Credentials come from env only and fail closed when absent.
//!
//! Python parity (`storage_service.write_bundle_sidecar`): key
//! `bundles/{app_id}/{version}/{sha256}.json` in `BUNDLE_BUCKET_NAME` (default
//! `waddles-bundles`), body = compact sorted-key ASCII JSON, `Content-Type: application/json`,
//! `x-amz-server-side-encryption: AES256`.

use object_store::aws::{AmazonS3Builder, AmazonS3ConfigKey};
use object_store::{Attribute, Attributes, ObjectStore, PutOptions, PutPayload};

use crate::signing::SignedSidecar;

/// Storage failure (secret-free text).
#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    #[error("{0}")]
    Config(String),
    #[error("sidecar upload failed: {0}")]
    Upload(String),
}

/// `bundles/{app_id}/{version}/{sha256}.json`.
pub fn bundle_sidecar_key(app_id: &str, version: &str, sha256_hex: &str) -> String {
    format!("bundles/{app_id}/{version}/{sha256_hex}.json")
}

/// The exact bytes uploaded for a signed sidecar (Python: `json.dumps(sort_keys, compact)`).
pub fn sidecar_body(doc: &SignedSidecar) -> Result<Vec<u8>, StorageError> {
    doc.to_canonical_json()
        .map(String::into_bytes)
        .map_err(|e| StorageError::Upload(e.to_string()))
}

/// Builds the S3 client from env. `OBJECT_STORAGE_KMS_ENABLED` is refused (fail-closed): the
/// SSE-KMS write needs hub-api's licence-gated entitlement check, which this CLI cannot make.
pub fn bundle_store_from_env() -> Result<(Box<dyn ObjectStore>, String), StorageError> {
    let get = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    let kms = get("OBJECT_STORAGE_KMS_ENABLED")
        .map(|v| {
            matches!(
                v.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false);
    if kms {
        return Err(StorageError::Config(
            "OBJECT_STORAGE_KMS_ENABLED is on: SSE-KMS writes require hub-api's entitlement \
             check; refusing to write under the baseline key instead -- use the Python CLI"
                .into(),
        ));
    }
    let access = get("S3_ACCESS_KEY_ID")
        .ok_or_else(|| StorageError::Config("S3_ACCESS_KEY_ID is not set".into()))?;
    let secret = get("S3_SECRET_ACCESS_KEY")
        .ok_or_else(|| StorageError::Config("S3_SECRET_ACCESS_KEY is not set".into()))?;
    let endpoint = get("S3_ENDPOINT_URL").unwrap_or_else(|| "http://infra-seaweedfs:8333".into());
    let region = get("S3_REGION").unwrap_or_else(|| "us-east-1".into());
    let bucket = get("BUNDLE_BUCKET_NAME").unwrap_or_else(|| "waddles-bundles".into());
    let sse_key: AmazonS3ConfigKey = "aws_server_side_encryption"
        .parse()
        .map_err(|_| StorageError::Config("object_store lacks SSE config key".into()))?;
    let store = AmazonS3Builder::new()
        .with_endpoint(&endpoint)
        .with_allow_http(endpoint.starts_with("http://"))
        .with_region(&region)
        .with_bucket_name(&bucket)
        .with_access_key_id(&access)
        .with_secret_access_key(&secret)
        // boto3 `s3v4` with a custom endpoint addresses path-style on SeaweedFS/MinIO.
        .with_virtual_hosted_style_request(false)
        .with_config(sse_key, "AES256")
        .build()
        .map_err(|_| StorageError::Config("could not build the S3 client from env".into()))?;
    Ok((Box::new(store), bucket))
}

/// Overwrites the bucket sidecar with the signed document; returns the object key.
pub async fn upload_signed_sidecar(
    store: &dyn ObjectStore,
    doc: &SignedSidecar,
) -> Result<String, StorageError> {
    let sha256_hex = doc.digest.strip_prefix("sha256:").unwrap_or(&doc.digest);
    let key = bundle_sidecar_key(&doc.app_id, &doc.version, sha256_hex);
    let body = sidecar_body(doc)?;
    let mut attributes = Attributes::new();
    attributes.insert(Attribute::ContentType, "application/json".into());
    let opts = PutOptions {
        attributes,
        ..Default::default()
    };
    let path = object_store::path::Path::parse(&key)
        .map_err(|e| StorageError::Upload(format!("invalid object key: {e}")))?;
    store
        .put_opts(&path, PutPayload::from(body), opts)
        .await
        .map_err(|e| StorageError::Upload(e.to_string()))?;
    Ok(key)
}
