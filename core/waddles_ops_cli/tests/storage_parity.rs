//! Sidecar upload parity: key layout, body bytes and content type match
//! `storage_service.write_bundle_sidecar` (Python). Uses `object_store`'s in-memory backend so
//! the exact `put` the S3 backend would sign is observed without a network.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use object_store::{path::Path, ObjectStoreExt};
use serde::Deserialize;
use waddles_ops_cli::signing::{PlatformSigner, SignedSidecar};
use waddles_ops_cli::storage::{bundle_sidecar_key, sidecar_body, upload_signed_sidecar};

#[derive(Deserialize)]
struct V {
    doc: SignedSidecar,
    body: String,
}

fn vectors() -> Vec<V> {
    serde_json::from_str(include_str!("sidecar_vectors.json")).unwrap()
}

#[derive(Deserialize)]
struct Sign {
    seed_b64: String,
    app_id: String,
    version: String,
    digest: String,
    approval_id: i64,
    sidecar: String,
}

#[test]
fn sidecar_body_is_byte_identical_including_non_ascii_digest() {
    let vs = vectors();
    assert_eq!(vs.len(), 2, "zero/short denominator");
    for v in &vs {
        assert_eq!(
            String::from_utf8(sidecar_body(&v.doc).unwrap()).unwrap(),
            v.body
        );
        assert!(v.body.is_ascii());
    }
}

#[tokio::test]
async fn upload_writes_python_key_and_bytes() {
    let signs: Vec<Sign> = serde_json::from_str(include_str!("python_vectors.json")).unwrap();
    assert_eq!(signs.len(), 3);
    for v in signs {
        let store = object_store::memory::InMemory::new();
        let doc = PlatformSigner::new(&v.seed_b64, "k1")
            .unwrap()
            .sign(&v.app_id, &v.version, &v.digest, v.approval_id)
            .unwrap();
        let key = upload_signed_sidecar(&store, &doc).await.unwrap();
        let sha = v.digest.strip_prefix("sha256:").unwrap_or(&v.digest);
        assert_eq!(
            key,
            format!("bundles/{}/{}/{sha}.json", v.app_id, v.version)
        );
        assert_eq!(key, bundle_sidecar_key(&v.app_id, &v.version, sha));
        let got = store.get(&Path::parse(&key).unwrap()).await.unwrap();
        assert_eq!(
            got.attributes
                .get(&object_store::Attribute::ContentType)
                .map(|a| a.as_ref()),
            Some("application/json")
        );
        let bytes = got.bytes().await.unwrap();
        assert_eq!(
            bytes.as_ref(),
            v.sidecar.as_bytes(),
            "uploaded bytes diverge from Python"
        );
    }
}

#[test]
fn kms_mode_and_missing_creds_fail_closed() {
    // Env is process-global: keep everything in this single test body.
    for k in [
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "OBJECT_STORAGE_KMS_ENABLED",
    ] {
        std::env::remove_var(k);
    }
    assert!(
        waddles_ops_cli::storage::bundle_store_from_env().is_err(),
        "no creds => error"
    );
    std::env::set_var("S3_ACCESS_KEY_ID", "x");
    assert!(
        waddles_ops_cli::storage::bundle_store_from_env().is_err(),
        "no secret => error"
    );
    std::env::set_var("S3_SECRET_ACCESS_KEY", "y");
    assert!(waddles_ops_cli::storage::bundle_store_from_env().is_ok());
    std::env::set_var("OBJECT_STORAGE_KMS_ENABLED", "true");
    let err = waddles_ops_cli::storage::bundle_store_from_env()
        .err()
        .unwrap()
        .to_string();
    assert!(err.contains("KMS"), "{err}");
}
