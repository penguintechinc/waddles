//! Real-path test against a live S3-compatible SeaweedFS. Runs only when
//! `BUNDLE_STORAGE_TEST_ENDPOINT` (+ `_BUCKET`, `_ACCESS_KEY`, `_SECRET_KEY`) is set --
//! `scripts/test-bundle-storage-real-path.sh` starts a container and sets them. The
//! test is `#[ignore]` so a plain `cargo test` never silently "passes" without a
//! backend; the script runs it with `--ignored` and fails if 0 tests ran.

use std::sync::Arc;

use bundle_host_storage::s3::{S3Backend, S3Config};
use bundle_host_storage::{GrantSet, StorageError, StorageHost, StorageScope, TracingAuditSink};
use bytes::Bytes;

fn env(k: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| panic!("{k} must be set for the real-path test"))
}

fn scope(app: &str, grants: &[&str]) -> StorageScope {
    StorageScope {
        app_id: app.into(),
        tenant: 1,
        community: 7,
        grants: GrantSet::from_permission_ids(grants.iter().copied()).expect("grants"),
        enabled: true,
        request_id: "real-1".into(),
    }
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "needs a live SeaweedFS; run via scripts/test-bundle-storage-real-path.sh"]
async fn seaweedfs_round_trip_tags_presign_and_tag_filter() {
    let backend = S3Backend::new(&S3Config {
        endpoint: env("BUNDLE_STORAGE_TEST_ENDPOINT"),
        bucket: env("BUNDLE_STORAGE_TEST_BUCKET"),
        region: "us-east-1".into(),
        access_key_id: env("BUNDLE_STORAGE_TEST_ACCESS_KEY"),
        secret_access_key: env("BUNDLE_STORAGE_TEST_SECRET_KEY"),
    })
    .expect("backend");
    let h = StorageHost::new(Arc::new(backend), Arc::new(TracingAuditSink));
    let run = format!("t{}", std::process::id());
    let owner = format!("harness.{run}");
    let consumer = format!("app.{run}");
    let o = scope(&owner, &[]);
    let c = scope(
        &consumer,
        &[
            &format!("storage.read:{owner}"),
            &format!("storage.list:{owner}"),
        ],
    );

    h.put(
        &o,
        None,
        "clips/a.mp4",
        Bytes::from_static(b"hello"),
        "video/mp4",
        &[],
    )
    .await
    .expect("put");
    h.put(
        &o,
        None,
        "clips/b.mp4",
        Bytes::from_static(b"world"),
        "video/mp4",
        &[],
    )
    .await
    .expect("put");
    assert_eq!(
        h.get(&o, None, "clips/a.mp4").await.expect("owner get").1,
        Bytes::from_static(b"hello")
    );
    // approved but untagged -> hidden
    assert_eq!(
        h.get(&c, Some(&owner), "clips/a.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert!(h
        .list(&c, Some(&owner), "clips", None, 10)
        .await
        .expect("list")
        .objects
        .is_empty());
    // owner tags a.mp4 for the consumer -> visible; b.mp4 stays hidden
    h.tag_add(&o, None, "clips/a.mp4", &consumer)
        .await
        .expect("tag");
    assert_eq!(
        h.get(&c, Some(&owner), "clips/a.mp4")
            .await
            .expect("consumer get")
            .1,
        Bytes::from_static(b"hello")
    );
    let page = h
        .list(&c, Some(&owner), "clips", None, 10)
        .await
        .expect("list");
    assert_eq!(
        page.objects
            .iter()
            .map(|x| x.key.as_str())
            .collect::<Vec<_>>(),
        vec!["clips/a.mp4"]
    );
    assert_eq!(
        h.get(&c, Some(&owner), "clips/b.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    // presign: works, URL scoped to the one object and expires
    let url = h
        .presign_get(&c, Some(&owner), "clips/a.mp4", 2)
        .await
        .expect("presign");
    assert!(
        url.contains("clips/a.mp4") && url.contains("X-Amz-Expires=2"),
        "{url}"
    );
    let ok = reqwest_get(&url).await;
    assert_eq!(ok, (200, "hello".to_string()));
    tokio::time::sleep(std::time::Duration::from_secs(4)).await;
    assert_ne!(reqwest_get(&url).await.0, 200, "expired URL must not work");
    // untag -> hidden again; traversal refused; tenant mismatch hidden
    h.tag_remove(&o, None, "clips/a.mp4", &consumer)
        .await
        .expect("untag");
    assert_eq!(
        h.get(&c, Some(&owner), "clips/a.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert!(matches!(
        h.get(&o, None, "../x").await,
        Err(StorageError::Invalid(_))
    ));
    let mut other_tenant = o.clone();
    other_tenant.tenant = 2;
    assert_eq!(
        h.get(&other_tenant, None, "clips/a.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    h.delete(&o, None, "clips/a.mp4").await.expect("del");
    h.delete(&o, None, "clips/b.mp4").await.expect("del");
}

/// Minimal HTTP GET over a raw TCP socket (no extra dev-dependency).
async fn reqwest_get(url: &str) -> (u16, String) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let u = url::Url::parse(url).expect("url");
    let host = u.host_str().expect("host");
    let port = u.port().unwrap_or(80);
    let mut s = tokio::net::TcpStream::connect((host, port))
        .await
        .expect("connect");
    let req = format!(
        "GET {}?{} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n",
        u.path(),
        u.query().unwrap_or("")
    );
    s.write_all(req.as_bytes()).await.expect("write");
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).await.expect("read");
    let text = String::from_utf8_lossy(&buf).to_string();
    let code = text
        .split_whitespace()
        .nth(1)
        .and_then(|c| c.parse().ok())
        .expect("status");
    (
        code,
        text.split("\r\n\r\n").nth(1).unwrap_or("").to_string(),
    )
}
