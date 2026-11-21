//! Defence in depth: the zero tenant/community is the fail-closed "unresolved" scope and
//! must be refused on every op before the backend is touched.

use std::sync::Arc;

use bundle_host_storage::{
    GrantSet, MemoryBackend, StorageError, StorageHost, StorageScope, TracingAuditSink,
};
use bytes::Bytes;

const APP: &str = "penguintechinc.waddles.harness-media";

fn scope(tenant: u64, community: u64) -> StorageScope {
    StorageScope {
        app_id: APP.into(),
        tenant,
        community,
        grants: GrantSet::default(),
        enabled: true,
        request_id: "req-1".into(),
    }
}

fn is_invalid<T: std::fmt::Debug>(r: Result<T, StorageError>) -> bool {
    matches!(r, Err(StorageError::Invalid(_)))
}

#[tokio::test]
async fn unresolved_zero_scope_is_refused_on_every_op() {
    let h = StorageHost::with_rate_limit(
        Arc::new(MemoryBackend::new()),
        Arc::new(TracingAuditSink),
        1000,
    );
    for (tenant, community) in [(0u64, 7u64), (1, 0), (0, 0)] {
        let s = scope(tenant, community);
        let put = h
            .put(
                &s,
                None,
                "k.mp4",
                Bytes::from_static(b"x"),
                "video/mp4",
                &[],
            )
            .await;
        assert!(is_invalid(put), "put {tenant}/{community}");
        assert!(is_invalid(h.get(&s, None, "k.mp4").await), "get");
        assert!(is_invalid(h.head(&s, None, "k.mp4").await), "head");
        assert!(is_invalid(h.delete(&s, None, "k.mp4").await), "delete");
        assert!(is_invalid(h.list(&s, None, "", None, 10).await), "list");
        assert!(
            is_invalid(h.tag_add(&s, None, "k.mp4", "penguintechinc.waddles.app-clips").await),
            "tag_add"
        );
        assert!(
            is_invalid(h.tag_remove(&s, None, "k.mp4", "penguintechinc.waddles.app-clips").await),
            "tag_remove"
        );
    }
}

#[tokio::test]
async fn resolved_scope_still_works() {
    let h = StorageHost::with_rate_limit(
        Arc::new(MemoryBackend::new()),
        Arc::new(TracingAuditSink),
        1000,
    );
    let s = scope(1, 7);
    h.put(
        &s,
        None,
        "k.mp4",
        Bytes::from_static(b"x"),
        "video/mp4",
        &[],
    )
    .await
    .expect("resolved scope is allowed");
}
